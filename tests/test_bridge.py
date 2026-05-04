"""Bridge translates SDK hook callbacks into subprocess invocations of devflow-lite hooks."""

from __future__ import annotations

from pathlib import Path

import pytest

from devflow_agent.bridge import build_hook_callback


@pytest.fixture
def fake_hook_dir(tmp_path: Path) -> Path:
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    # Hook that echoes its stdin to stdout as a hookSpecificOutput context.
    (hooks / "echo_hook.py").write_text(
        "import json, sys\n"
        "data = json.loads(sys.stdin.read() or '{}')\n"
        "print(json.dumps({'hookSpecificOutput': {"
        "'hookEventName': data.get('hook_event_name', 'unknown'),"
        "'additionalContext': 'tool=' + data.get('tool_name', '')}}))\n"
    )
    # Hook that blocks.
    (hooks / "block_hook.py").write_text(
        "import json, sys\n"
        "print(json.dumps({'decision': 'block', 'reason': 'denied for testing'}))\n"
    )
    # Hook that exits 0 with empty stdout (no-op).
    (hooks / "noop_hook.py").write_text("import sys; sys.exit(0)\n")
    return hooks


@pytest.mark.asyncio
async def test_bridge_passes_input_as_stdin_json(fake_hook_dir):
    callback = build_hook_callback(
        ["echo_hook"], hooks_dir=fake_hook_dir, devflow_root=fake_hook_dir.parent
    )
    result = await callback(
        {"session_id": "s1", "tool_name": "Bash", "hook_event_name": "PreToolUse"},
        "tool-use-id-1",
        {"signal": None},
    )
    assert result["hookSpecificOutput"]["additionalContext"] == "tool=Bash"


@pytest.mark.asyncio
async def test_bridge_returns_block_decision(fake_hook_dir):
    callback = build_hook_callback(
        ["block_hook"], hooks_dir=fake_hook_dir, devflow_root=fake_hook_dir.parent
    )
    result = await callback(
        {"session_id": "s1", "hook_event_name": "PreToolUse"}, None, {"signal": None}
    )
    assert result["decision"] == "block"
    assert result["reason"] == "denied for testing"


@pytest.mark.asyncio
async def test_bridge_short_circuits_on_first_block(fake_hook_dir):
    callback = build_hook_callback(
        ["block_hook", "echo_hook"], hooks_dir=fake_hook_dir, devflow_root=fake_hook_dir.parent
    )
    result = await callback(
        {"session_id": "s1", "hook_event_name": "PreToolUse"}, None, {"signal": None}
    )
    # block_hook should win — echo_hook never runs
    assert result["decision"] == "block"


@pytest.mark.asyncio
async def test_bridge_returns_empty_when_all_hooks_silent(fake_hook_dir):
    callback = build_hook_callback(
        ["noop_hook"], hooks_dir=fake_hook_dir, devflow_root=fake_hook_dir.parent
    )
    result = await callback(
        {"session_id": "s1", "hook_event_name": "PreToolUse"}, None, {"signal": None}
    )
    assert result == {}


@pytest.mark.asyncio
async def test_bridge_sets_devflow_env(fake_hook_dir):
    # Hook that prints DEVFLOW_SESSION_ID + DEVFLOW_ROOT it received via env.
    (fake_hook_dir / "env_hook.py").write_text(
        "import json, os\n"
        "print(json.dumps({'hookSpecificOutput': {"
        "'hookEventName': 'PostToolUse',"
        "'additionalContext': os.environ['DEVFLOW_SESSION_ID']+'|'+os.environ['DEVFLOW_ROOT']}}))\n"
    )
    callback = build_hook_callback(
        ["env_hook"], hooks_dir=fake_hook_dir, devflow_root=fake_hook_dir.parent
    )
    result = await callback(
        {"session_id": "abc-123", "hook_event_name": "PostToolUse"}, None, {"signal": None}
    )
    expected = f"abc-123|{fake_hook_dir.parent}"
    assert result["hookSpecificOutput"]["additionalContext"] == expected
