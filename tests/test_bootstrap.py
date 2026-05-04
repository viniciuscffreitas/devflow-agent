"""Bootstrap runs SessionStart-equivalent hooks once and aggregates their context."""

from __future__ import annotations

from pathlib import Path

import pytest

from devflow_agent.bootstrap import collect_session_context


def _write_context_hook(hooks_dir: Path, name: str, context: str) -> None:
    (hooks_dir / f"{name}.py").write_text(
        "import json\n"
        f"print(json.dumps({{'hookSpecificOutput': {{'hookEventName': 'SessionStart', "
        f"'additionalContext': {context!r}}}}}))\n"
    )


@pytest.mark.asyncio
async def test_collect_session_context_concatenates_in_order(tmp_path: Path):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    _write_context_hook(hooks, "first", "[devflow:profile] python")
    _write_context_hook(hooks, "second", "[devflow:freshness] up-to-date")

    contexts = await collect_session_context(
        ["first", "second"],
        hooks_dir=hooks,
        devflow_root=tmp_path,
        session_id="boot-1",
        cwd=tmp_path,
    )
    assert contexts == ["[devflow:profile] python", "[devflow:freshness] up-to-date"]


@pytest.mark.asyncio
async def test_collect_skips_silent_hooks(tmp_path: Path):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "silent.py").write_text("import sys; sys.exit(0)\n")
    _write_context_hook(hooks, "loud", "loaded")

    contexts = await collect_session_context(
        ["silent", "loud"],
        hooks_dir=hooks,
        devflow_root=tmp_path,
        session_id="boot-2",
        cwd=tmp_path,
    )
    assert contexts == ["loaded"]


@pytest.mark.asyncio
async def test_bootstrap_passes_session_start_payload(tmp_path: Path):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    # Hook that echoes the hook_event_name + cwd it received.
    (hooks / "introspect.py").write_text(
        "import json, sys\n"
        "data = json.loads(sys.stdin.read() or '{}')\n"
        "print(json.dumps({'hookSpecificOutput': {"
        "'hookEventName': 'SessionStart',"
        "'additionalContext': data['hook_event_name']+'@'+data['cwd']}}))\n"
    )
    target = tmp_path / "project"
    target.mkdir()
    contexts = await collect_session_context(
        ["introspect"], hooks_dir=hooks, devflow_root=tmp_path, session_id="boot-3", cwd=target
    )
    assert contexts == [f"SessionStart@{target}"]
