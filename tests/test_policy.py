"""Destructive-op auto-policy intercepts dangerous Bash commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from devflow_agent.policy import (
    DESTRUCTIVE_PATTERNS,
    Policy,
    build_policy_callback,
)


def _input(cmd: str, session: str = "s1") -> dict:
    return {
        "session_id": session,
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "hook_event_name": "PreToolUse",
    }


@pytest.mark.asyncio
async def test_auto_deny_blocks_rm_rf(tmp_path: Path):
    cb = build_policy_callback(Policy.AUTO_DENY, state_root=tmp_path)
    response = await cb(_input("rm -rf /tmp/something"), None, {"signal": None})
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "rm -rf" in response["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.asyncio
async def test_auto_deny_allows_safe_bash(tmp_path: Path):
    cb = build_policy_callback(Policy.AUTO_DENY, state_root=tmp_path)
    response = await cb(_input("ls -la"), None, {"signal": None})
    assert response == {}


@pytest.mark.asyncio
async def test_log_and_allow_writes_log(tmp_path: Path):
    cb = build_policy_callback(Policy.LOG_AND_ALLOW, state_root=tmp_path)
    inp = _input("git push --force origin main", session="abc")
    response = await cb(inp, None, {"signal": None})
    assert response == {}
    log = tmp_path / "state" / "abc" / "destructive.log"
    assert log.exists()
    line = log.read_text().strip().splitlines()[-1]
    record = json.loads(line)
    assert record["matched_pattern"] == "git push --force"
    assert record["command"] == "git push --force origin main"


@pytest.mark.asyncio
async def test_escalate_returns_block_with_marker(tmp_path: Path):
    cb = build_policy_callback(Policy.ESCALATE, state_root=tmp_path)
    response = await cb(_input("DROP TABLE users;"), None, {"signal": None})
    assert response["decision"] == "block"
    assert response["reason"].startswith("[devflow_agent:ESCALATE]")
    assert "DROP TABLE" in response["reason"]


@pytest.mark.asyncio
async def test_non_bash_tool_passes_through(tmp_path: Path):
    cb = build_policy_callback(Policy.AUTO_DENY, state_root=tmp_path)
    response = await cb(
        {
            "session_id": "s",
            "tool_name": "Edit",
            "tool_input": {"file_path": "/tmp/x"},
            "hook_event_name": "PreToolUse",
        },
        None,
        {"signal": None},
    )
    assert response == {}


def test_destructive_patterns_are_regex_compilable():
    import re

    for pattern in DESTRUCTIVE_PATTERNS:
        re.compile(pattern.regex)
