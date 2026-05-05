"""PreToolUse hook enforcing per-repo base-branch policy on ``gh pr create``.

WORKFLOW.md soft prompts can ask the agent to target ``dev`` instead of
``main``, but the agent ignored that in 8/8 stress-test runs. This hook
intercepts the actual Bash invocation and denies any ``gh pr create`` whose
``--base`` flag conflicts with the orchestrator-supplied policy.

Policy source order:

1. Explicit ``policy`` kwarg passed to :func:`build_pr_base_branch_callback`.
2. ``SYMPHONY_PR_BASE_POLICY`` env var (JSON object ``{owner/repo: branch}``)
   re-read on every invocation so the orchestrator can rotate policy without
   restarting the SDK.

Repo source order (``--repo`` lookup):

1. Explicit ``--repo owner/name`` (or ``--repo=owner/name``) on the command.
2. ``cd <subdir> && gh pr create ...`` — leading ``cd`` resolves against
   ``cwd`` and the hook reads ``git remote get-url origin`` from there.
3. The ``cwd`` kwarg passed to :func:`build_pr_base_branch_callback` — same
   git lookup, applied when the agent runs ``gh pr create`` directly inside
   the workspace root.

If neither the policy nor the repo can be resolved, the callback is a no-op —
never blocks unrelated workflows.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

HookInput = dict[str, Any]
HookOutput = dict[str, Any]
HookCallback = Callable[[HookInput, str | None, dict], Awaitable[HookOutput]]

_ENV_VAR = "SYMPHONY_PR_BASE_POLICY"

_GITHUB_URL_RE = re.compile(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")

_DEBUG_LOG = os.environ.get("SYMPHONY_PR_BASE_DEBUG_LOG")


def _debug_log_all(input: HookInput, base_cwd: Path | None) -> None:
    if not _DEBUG_LOG:
        return
    try:
        with open(_DEBUG_LOG, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps({"input": input, "base_cwd": str(base_cwd) if base_cwd else None}) + "\n"
            )
    except OSError:
        pass


def _parse_gh_pr_create(command: str) -> dict[str, Any] | None:
    """Extract ``--repo``, ``--base`` and any leading ``cd <dir> &&`` prefix.

    Tolerates pipes/``&&``/``;`` separators by stopping at the next operator
    once it locates the ``gh pr create`` token sequence. Returns ``None`` if
    the command does not actually invoke ``gh pr create``.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None

    start: int | None = None
    for i in range(len(tokens) - 2):
        if tokens[i] == "gh" and tokens[i + 1] == "pr" and tokens[i + 2] == "create":
            start = i + 3
            break
    if start is None:
        return None

    cd_target: str | None = None
    if start >= 6 and tokens[0] == "cd" and tokens[2] == "&&":
        cd_target = tokens[1]

    repo: str | None = None
    base: str | None = None
    j = start
    while j < len(tokens):
        tok = tokens[j]
        if tok in {"&&", "||", ";", "|"}:
            break
        if tok == "--repo" and j + 1 < len(tokens):
            repo = tokens[j + 1]
            j += 2
            continue
        if tok.startswith("--repo="):
            repo = tok.split("=", 1)[1]
            j += 1
            continue
        if tok == "--base" and j + 1 < len(tokens):
            base = tokens[j + 1]
            j += 2
            continue
        if tok.startswith("--base="):
            base = tok.split("=", 1)[1]
            j += 1
            continue
        j += 1
    return {"repo": repo, "base": base, "cd_target": cd_target}


def _load_policy_from_env() -> dict[str, str]:
    raw = os.environ.get(_ENV_VAR)
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {str(k): str(v) for k, v in loaded.items() if isinstance(v, str)}


async def _detect_repo_from_git(cwd: Path) -> str | None:
    """Run ``git remote get-url origin`` in ``cwd`` and return ``owner/repo``."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(cwd),
            "remote",
            "get-url",
            "origin",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
    except (OSError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    url = stdout.decode().strip()
    match = _GITHUB_URL_RE.search(url)
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}"


def _resolve_effective_cwd(base_cwd: Path | None, cd_target: str | None) -> Path | None:
    if base_cwd is None and cd_target is None:
        return None
    if cd_target is None:
        return base_cwd
    target = Path(cd_target)
    if target.is_absolute() or base_cwd is None:
        return target
    return (base_cwd / target).resolve()


def build_pr_base_branch_callback(
    policy: dict[str, str] | None = None,
    *,
    cwd: Path | None = None,
) -> HookCallback:
    """Return a PreToolUse callback enforcing per-repo base-branch policy.

    Args:
        policy: explicit ``{owner/repo: required_base}`` mapping. When ``None``,
            ``SYMPHONY_PR_BASE_POLICY`` is re-read on every invocation so the
            orchestrator can rotate policy without restarting the SDK.
        cwd: workspace root. Used to detect ``owner/repo`` via
            ``git remote get-url origin`` when the command omits ``--repo``.
            Honors a leading ``cd <subdir> &&`` so multi-repo workspaces
            (e.g. ``./fe-next-app`` next to a primary repo) resolve correctly.

    Denies ``gh pr create`` when the resolved repo matches a policy entry and
    ``--base`` is missing or differs from the required branch. Otherwise
    returns ``{}`` (allow).
    """
    explicit_policy = dict(policy) if policy is not None else None
    base_cwd = Path(cwd).resolve() if cwd is not None else None

    async def callback(input: HookInput, tool_use_id: str | None, context: dict) -> HookOutput:
        _debug_log_all(input, base_cwd)
        if input.get("tool_name") != "Bash":
            return {}
        command = (input.get("tool_input") or {}).get("command") or ""
        if "gh pr create" not in command:
            return {}

        active_policy = explicit_policy if explicit_policy is not None else _load_policy_from_env()
        if not active_policy:
            return {}

        parsed = _parse_gh_pr_create(command)
        if parsed is None:
            return {}

        repo: str | None = parsed["repo"]
        if not repo:
            effective_cwd = _resolve_effective_cwd(base_cwd, parsed["cd_target"])
            if effective_cwd is not None:
                repo = await _detect_repo_from_git(effective_cwd)

        if not repo or repo not in active_policy:
            return {}

        required = active_policy[repo]
        actual = parsed["base"]
        if actual == required:
            return {}

        if actual is None:
            reason = (
                f"devflow_agent.pr_base_branch: repo {repo} requires "
                f"--base {required} per SYMPHONY_PR_BASE_POLICY. "
                f"Add --base {required} to the gh pr create command."
            )
        else:
            reason = (
                f"devflow_agent.pr_base_branch: repo {repo} requires "
                f"--base {required} per SYMPHONY_PR_BASE_POLICY, "
                f"got --base {actual}."
            )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    return callback
