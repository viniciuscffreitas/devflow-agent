"""High-level builder wires bridge + bootstrap + spec_seed into ClaudeAgentOptions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from devflow_agent.config import DevflowBundle, build_options, compose_devflow_bundle
from devflow_agent.policy import Policy
from devflow_agent.spec_seed import IssueContext


def _seed_lite(devflow_root: Path) -> None:
    """Write a minimal lite hooks dir that build_options can resolve."""
    hooks = devflow_root / "hooks"
    hooks.mkdir(parents=True)
    for name in (
        "secrets_gate",
        "pre_push_gate",
        "commit_validator",
        "tdd_enforcer",
        "file_checker",
        "context_monitor",
        "pre_compact",
        "post_compact_restore",
        "spec_stop_guard",
        "phase_finalize",
        "stop_dispatcher",
        "pre_edit_overwrite_guard",
        "concurrent_edit_lock",
        "discovery_scan",
        "freshness_check",
        "repo_conventions",
        "state_cleanup",
        "branch_policy",
        "merge_safety",
        "codeowners_check",
    ):
        (hooks / f"{name}.py").write_text("import sys; sys.exit(0)\n")


@pytest.mark.asyncio
async def test_build_options_returns_claude_agent_options(tmp_path: Path):
    _seed_lite(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    issue = IssueContext(identifier="X-1", title="t", url="u")
    options, session_id = await build_options(
        issue=issue,
        cwd=repo,
        devflow_root=tmp_path,
        policy=Policy.AUTO_DENY,
        base_system_prompt="You are an agent.",
    )
    from claude_agent_sdk import ClaudeAgentOptions  # noqa: F401
    assert isinstance(session_id, str) and session_id
    assert hasattr(options, "hooks")
    assert "PreToolUse" in options.hooks
    assert "PostToolUse" in options.hooks
    assert "Stop" in options.hooks
    assert "You are an agent." in options.system_prompt


@pytest.mark.asyncio
async def test_build_options_writes_implementing_marker(tmp_path: Path):
    _seed_lite(tmp_path)
    issue = IssueContext(identifier="Y-2", title="x", url="")
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _, session_id = await build_options(
        issue=issue,
        cwd=cwd,
        devflow_root=tmp_path,
        policy=Policy.AUTO_DENY,
        base_system_prompt="",
    )
    marker = tmp_path / "state" / session_id / "active-spec.json"
    assert marker.exists()
    data = json.loads(marker.read_text())
    assert data["status"] == "IMPLEMENTING"
    assert data["plan_path"] == "Y-2: x"


@pytest.mark.asyncio
async def test_build_options_resolves_correct_hook_groups(tmp_path: Path):
    _seed_lite(tmp_path)
    issue = IssueContext(identifier="Z-3", title="t", url="")
    options, _ = await build_options(
        issue=issue,
        cwd=tmp_path,
        devflow_root=tmp_path,
        policy=Policy.AUTO_DENY,
        base_system_prompt="",
    )
    pre_tool = options.hooks["PreToolUse"]
    post_tool = options.hooks["PostToolUse"]
    stop = options.hooks["Stop"]
    pre_compact = options.hooks["PreCompact"]
    assert len(pre_tool) >= 1
    assert len(post_tool) >= 1
    assert len(stop) == 1
    assert len(pre_compact) == 1


@pytest.mark.asyncio
async def test_compose_devflow_bundle_returns_pieces_for_caller_owned_options(
    tmp_path: Path,
):
    """Caller-side composition path: bundle has session_id + system_prompt + hooks."""
    _seed_lite(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    issue = IssueContext(identifier="C-1", title="compose", url="")
    bundle = await compose_devflow_bundle(
        issue=issue,
        cwd=repo,
        devflow_root=tmp_path,
        policy=Policy.AUTO_DENY,
        base_system_prompt="caller prompt",
    )
    assert isinstance(bundle, DevflowBundle)
    assert isinstance(bundle.session_id, str) and bundle.session_id
    assert "caller prompt" in (bundle.system_prompt or "")
    assert set(bundle.hooks.keys()) == {"PreToolUse", "PostToolUse", "Stop", "PreCompact"}
    marker = tmp_path / "state" / bundle.session_id / "active-spec.json"
    assert marker.exists()


@pytest.mark.asyncio
async def test_compose_devflow_bundle_no_base_prompt_returns_none_or_contexts(
    tmp_path: Path,
):
    """When base_system_prompt='' and bootstrap silent, system_prompt is None."""
    _seed_lite(tmp_path)
    issue = IssueContext(identifier="C-2", title="silent", url="")
    bundle = await compose_devflow_bundle(
        issue=issue,
        cwd=tmp_path,
        devflow_root=tmp_path,
        policy=Policy.AUTO_DENY,
        base_system_prompt="",
    )
    assert bundle.system_prompt is None
