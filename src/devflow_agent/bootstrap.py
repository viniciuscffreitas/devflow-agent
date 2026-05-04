"""Run SessionStart-equivalent devflow-lite hooks before the SDK starts.

The SDK exposes PreToolUse / PostToolUse / Stop / PreCompact / UserPromptSubmit
but not SessionStart. devflow-lite uses SessionStart for project introspection
(discovery_scan, freshness_check, repo_conventions, state_cleanup), which return
additionalContext strings the agent sees as system context.

We replicate that by running the same scripts via subprocess at boot, parsing
hookSpecificOutput.additionalContext from each, and returning the list so the
caller can inject it into ClaudeAgentOptions.system_prompt.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Iterable
from pathlib import Path

DEFAULT_BOOTSTRAP_HOOKS = (
    "state_cleanup",
    "discovery_scan",
    "repo_conventions",
    "freshness_check",
)


async def _invoke(
    script: str,
    *,
    hooks_dir: Path,
    devflow_root: Path,
    session_id: str,
    cwd: Path,
) -> str | None:
    script_path = hooks_dir / f"{script}.py"
    if not script_path.exists():
        print(f"[devflow_agent.bootstrap] missing: {script_path}", file=sys.stderr)
        return None

    payload = json.dumps(
        {
            "hook_event_name": "SessionStart",
            "session_id": session_id,
            "cwd": str(cwd),
            "transcript_path": "",
        }
    ).encode("utf-8")

    env = {
        **os.environ,
        "DEVFLOW_SESSION_ID": session_id,
        "DEVFLOW_ROOT": str(devflow_root),
        "CLAUDE_SESSION_ID": session_id,
    }
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=str(cwd),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(payload), timeout=60)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        print(f"[devflow_agent.bootstrap] timeout: {script}", file=sys.stderr)
        return None

    if stderr:
        sys.stderr.write(stderr.decode("utf-8", errors="replace"))

    out = stdout.decode("utf-8", errors="replace").strip()
    if not out:
        return None
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        return None
    hsp = parsed.get("hookSpecificOutput") or {}
    ctx = hsp.get("additionalContext")
    if isinstance(ctx, str) and ctx.strip():
        return ctx
    return None


async def collect_session_context(
    scripts: Iterable[str] = DEFAULT_BOOTSTRAP_HOOKS,
    *,
    hooks_dir: Path,
    devflow_root: Path,
    session_id: str,
    cwd: Path,
) -> list[str]:
    """Run each bootstrap hook in order and return the non-empty contexts.

    The hooks are run sequentially: discovery_scan writes a profile cache that
    repo_conventions reads. Order matters.
    """
    resolved_hooks = Path(hooks_dir).resolve()
    resolved_root = Path(devflow_root).resolve()
    resolved_cwd = Path(cwd).resolve()
    out: list[str] = []
    for script in scripts:
        context = await _invoke(
            script,
            hooks_dir=resolved_hooks,
            devflow_root=resolved_root,
            session_id=session_id,
            cwd=resolved_cwd,
        )
        if context:
            out.append(context)
    return out
