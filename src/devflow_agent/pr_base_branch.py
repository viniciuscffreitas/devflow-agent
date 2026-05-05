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

If neither is present (or the env var fails to parse), the callback is a
no-op — never blocks unrelated workflows.
"""

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Awaitable, Callable
from typing import Any

HookInput = dict[str, Any]
HookOutput = dict[str, Any]
HookCallback = Callable[[HookInput, str | None, dict], Awaitable[HookOutput]]


def _parse_gh_pr_create(command: str) -> dict[str, str | None] | None:
    """Extract ``--repo`` and ``--base`` from a shell command containing ``gh pr create``.

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
    return {"repo": repo, "base": base}


def _load_policy_from_env() -> dict[str, str]:
    raw = os.environ.get("SYMPHONY_PR_BASE_POLICY")
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {str(k): str(v) for k, v in loaded.items() if isinstance(v, str)}


def build_pr_base_branch_callback(
    policy: dict[str, str] | None = None,
) -> HookCallback:
    """Return a PreToolUse callback that enforces per-repo base-branch policy.

    Args:
        policy: explicit ``{owner/repo: required_base}`` mapping. When ``None``,
            ``SYMPHONY_PR_BASE_POLICY`` is re-read on every invocation so the
            orchestrator can rotate policy without restarting the SDK.

    Denies ``gh pr create`` when ``--repo`` matches a policy entry and
    ``--base`` is missing or differs from the required branch. Otherwise
    returns ``{}`` (allow).
    """
    explicit_policy = dict(policy) if policy is not None else None

    async def callback(input: HookInput, tool_use_id: str | None, context: dict) -> HookOutput:
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

        repo = parsed["repo"]
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
