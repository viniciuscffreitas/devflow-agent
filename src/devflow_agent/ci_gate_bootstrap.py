"""CI gate bootstrap: extract portable CI commands from GitHub Actions workflows.

On every agent session, reads .github/workflows/*.yml in the workspace,
extracts run: steps that are safe to execute locally (no secrets, no docker,
no deploy/publish steps), and writes .devflow/ci-commands.sh.

The build_ci_gate_callback PreToolUse hook runs that script before any
git push, blocking the push if any CI command fails.
"""

from __future__ import annotations

import asyncio
import re
import stat
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import yaml

HookInput = dict[str, Any]
HookOutput = dict[str, Any]
HookCallback = Callable[[HookInput, str | None, dict], Awaitable[HookOutput]]

# Job names that signal infrastructure/deploy work — skip entirely.
_SKIP_JOB_KEYWORDS = frozenset(
    [
        "deploy",
        "release",
        "publish",
        "upload",
        "push",
        "staging",
        "production",
        "notify",
        "notification",
        "infrastructure",
        "infra",
        "migrate",
        "migration",
    ]
)

# Unique sentinel replacing ${{ secrets.* }} — avoids collision with literal
# strings a run: step might contain (e.g. echo __GH_SECRET__).
_SECRET_SENTINEL = "__DEVFLOW_CI_SECRET_7e4a__"

# Substrings in a run: value that mark it as non-portable.
_SKIP_RUN_SUBSTRINGS = (
    "${{ secrets.",
    _SECRET_SENTINEL,
    "docker build",
    "docker push",
    "docker run",
    "kubectl",
    "helm ",
    "gcloud ",
    "aws ",
    "az ",
    "vercel ",
    "railway ",
    "fly ",
    "netlify ",
    "heroku ",
    "npm publish",
    "yarn publish",
    "gem push",
    "cargo publish",
    "twine upload",
    "pip publish",
)

_GH_EXPR_RE = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)


def _job_is_skippable(job_name: str, job_def: dict) -> bool:
    name_lower = job_name.lower()
    if any(kw in name_lower for kw in _SKIP_JOB_KEYWORDS):
        return True
    if job_def.get("services"):
        return True
    return False


def _run_is_portable(run_text: str) -> bool:
    for substr in _SKIP_RUN_SUBSTRINGS:
        if substr in run_text:
            return False
    return True


def _preprocess_workflow_yaml(raw: str) -> str:
    """Make GitHub Actions YAML safe for PyYAML.

    PyYAML is stricter than GitHub's parser: flow indicators (${{ }}) and
    colon-space inside plain scalars are parse errors. Fix both by substituting
    GH expressions and converting inline run: values to block literal form.
    """
    # Mark secrets with a sentinel so portability check still filters them.
    text = re.sub(r"\$\{\{\s*secrets\.[^}]*\}\}", _SECRET_SENTINEL, raw)
    # Neutralize all remaining GH template expressions.
    text = _GH_EXPR_RE.sub("__GH_EXPR__", text)

    # Convert inline run: scalars to block literal form (| notation) so that
    # strings containing ': ' or '"' don't trip YAML's mapping-value parser.
    # Skip values already using block/folded indicators (| or >).
    def _to_block(m: re.Match) -> str:
        indent, value = m.group(1), m.group(2).strip()
        return f"{indent}run: |\n{indent}  {value}"

    text = re.sub(r"^(\s+)run: (?![|>])(.+)$", _to_block, text, flags=re.MULTILINE)
    return text


def _extract_commands(workflow_path: Path) -> list[str]:
    try:
        raw = workflow_path.read_text(encoding="utf-8")
        doc = yaml.safe_load(_preprocess_workflow_yaml(raw))
    except Exception:  # noqa: BLE001
        return []

    if not isinstance(doc, dict):
        return []

    jobs = doc.get("jobs") or {}
    commands: list[str] = []

    for job_name, job_def in jobs.items():
        if not isinstance(job_def, dict):
            continue
        if _job_is_skippable(str(job_name), job_def):
            continue

        job_env = job_def.get("env") or {}
        for step in job_def.get("steps") or []:
            if not isinstance(step, dict):
                continue
            if "uses" in step:
                continue
            run = step.get("run")
            if not isinstance(run, str):
                continue
            run = run.strip()
            # Skip steps where a secret flows in via env: (indirect reference).
            step_env = step.get("env") or {}
            env_values = list(job_env.values()) + list(step_env.values())
            if any(_SECRET_SENTINEL in str(v) for v in env_values):
                continue
            if run and _run_is_portable(run):
                commands.append(run)

    return commands


def sync_ci_gate(cwd: Path) -> Path | None:
    """Read .github/workflows/*.yml, extract portable commands, write .devflow/ci-commands.sh.

    Always overwrites on re-run so the script stays in sync with CI.
    Returns the path to the written script, or None if nothing portable was found.
    """
    workflows_dir = cwd / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return None

    all_commands: list[str] = []
    for wf_file in sorted(workflows_dir.glob("*.yml")):
        all_commands.extend(_extract_commands(wf_file))
    for wf_file in sorted(workflows_dir.glob("*.yaml")):
        all_commands.extend(_extract_commands(wf_file))

    if not all_commands:
        return None

    out_dir = cwd / ".devflow"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "ci-commands.sh"

    lines = ["#!/usr/bin/env bash", "set -euo pipefail", ""]
    for cmd in all_commands:
        lines.append(cmd)
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    out_path.chmod(out_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    return out_path


def build_ci_gate_callback(cwd: Path | None = None) -> HookCallback:
    """Return a PreToolUse callback that runs .devflow/ci-commands.sh before git push.

    If the script exits non-zero, the push is denied with the captured output.
    If no cwd is given or the script doesn't exist, the callback is a no-op.
    """
    base_cwd = Path(cwd).resolve() if cwd is not None else None

    async def callback(input: HookInput, tool_use_id: str | None, context: dict) -> HookOutput:
        if input.get("tool_name") != "Bash":
            return {}
        command = (input.get("tool_input") or {}).get("command") or ""
        if "git push" not in command:
            return {}
        if base_cwd is None:
            return {}

        ci_script = base_cwd / ".devflow" / "ci-commands.sh"
        if not ci_script.exists():
            return {}

        try:
            proc = await asyncio.create_subprocess_exec(
                str(ci_script),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(base_cwd),
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            "ci_gate: CI commands timed out after 300s. Fix CI before pushing."
                        ),
                    }
                }
        except (OSError, ValueError) as exc:
            print(f"[ci_gate] failed to run {ci_script}: {exc}", file=sys.stderr)
            return {}

        if proc.returncode == 0:
            return {}

        output = stdout.decode("utf-8", errors="replace").strip()
        reason = f"ci_gate: CI commands failed (exit {proc.returncode})."
        if output:
            # Trim to last 2000 chars to stay within reason field limits
            tail = output[-2000:] if len(output) > 2000 else output
            reason = f"{reason}\n\n{tail}"
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    return callback
