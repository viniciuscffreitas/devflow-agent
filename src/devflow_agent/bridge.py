"""SDK hook callback → devflow-lite subprocess adapter.

claude-agent-sdk expects hook callbacks with shape::

    async def cb(input: HookInput, tool_use_id: str | None, ctx: HookContext) -> HookJSONOutput

devflow-lite hooks expect a JSON payload on stdin and write a JSON response on
stdout — same schema as Claude Code emits. ``build_hook_callback`` wires the
two together: it forwards ``input`` as stdin JSON, propagates per-session env
vars (DEVFLOW_SESSION_ID, DEVFLOW_ROOT), and parses stdout back into the
SDK's ``HookJSONOutput`` shape.

Multiple scripts can be chained in one matcher. The first one that returns a
``decision == "block"`` or ``permissionDecision == "deny"`` short-circuits the
chain — same behavior as Claude Code's hook executor.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

HookInput = dict[str, Any]
HookOutput = dict[str, Any]
HookCallback = Callable[[HookInput, str | None, dict], Awaitable[HookOutput]]


def _is_blocking(response: HookOutput) -> bool:
    if response.get("decision") == "block":
        return True
    hsp = response.get("hookSpecificOutput") or {}
    if hsp.get("permissionDecision") == "deny":
        return True
    return False


async def _run_one(
    script_name: str, hooks_dir: Path, devflow_root: Path, payload: bytes, session_id: str
) -> HookOutput:
    script_path = hooks_dir / f"{script_name}.py"
    if not script_path.exists():
        # Don't kill the agent over a missing hook — log and continue.
        print(
            f"[devflow_agent.bridge] missing hook script: {script_path}",
            file=sys.stderr,
        )
        return {}

    env = {
        **os.environ,
        "DEVFLOW_SESSION_ID": session_id,
        "DEVFLOW_ROOT": str(devflow_root),
        "CLAUDE_SESSION_ID": session_id,  # devflow-lite reads either name
    }
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(payload), timeout=30)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        print(
            f"[devflow_agent.bridge] hook timed out: {script_name}",
            file=sys.stderr,
        )
        return {}

    if stderr:
        # devflow-lite hooks log diagnostics to stderr — surface them but never block.
        sys.stderr.write(stderr.decode("utf-8", errors="replace"))

    out = stdout.decode("utf-8", errors="replace").strip()
    if not out:
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        print(
            f"[devflow_agent.bridge] hook returned non-JSON stdout: {script_name}: {out[:200]!r}",
            file=sys.stderr,
        )
        return {}


def build_hook_callback(scripts: list[str], *, hooks_dir: Path, devflow_root: Path) -> HookCallback:
    """Return an async hook callback that runs ``scripts`` in order.

    Args:
        scripts: hook script names without ``.py`` (e.g. ``["secrets_gate"]``).
        hooks_dir: directory containing the script files.
        devflow_root: passed via ``DEVFLOW_ROOT`` env var so devflow-lite's
            ``_paths.py`` resolves the right state/telemetry/log directories.

    The first hook that returns a blocking response wins — later hooks do not
    run, and that response is returned to the SDK verbatim.
    """
    script_list = list(scripts)
    hooks_dir = Path(hooks_dir).resolve()
    devflow_root = Path(devflow_root).resolve()

    async def callback(input: HookInput, tool_use_id: str | None, context: dict) -> HookOutput:
        session_id = input.get("session_id") or "default"
        payload = json.dumps(input).encode("utf-8")
        last: HookOutput = {}
        for script in script_list:
            response = await _run_one(script, hooks_dir, devflow_root, payload, session_id)
            if _is_blocking(response):
                return response
            if response:
                last = response
        return last

    return callback
