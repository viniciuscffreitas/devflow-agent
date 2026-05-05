"""PreToolUse hook gates ``gh pr create`` against per-repo base-branch policy.

Soft prompts in WORKFLOW.md don't enforce; this hook does. Symphony loads
``SYMPHONY_PR_BASE_POLICY`` from the workflow file and the hook denies any
``gh pr create`` whose ``--base`` doesn't match (or is absent for a policy repo).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from devflow_agent.pr_base_branch import build_pr_base_branch_callback


def _input(cmd: str) -> dict:
    return {
        "session_id": "s1",
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "hook_event_name": "PreToolUse",
    }


@pytest.mark.asyncio
async def test_no_policy_passes_through_any_command():
    cb = build_pr_base_branch_callback(policy={})
    response = await cb(_input("gh pr create --repo any/repo --base main"), None, {})
    assert response == {}


@pytest.mark.asyncio
async def test_non_bash_tool_passes_through():
    cb = build_pr_base_branch_callback(policy={"foo/bar": "dev"})
    response = await cb(
        {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/x"}},
        None,
        {},
    )
    assert response == {}


@pytest.mark.asyncio
async def test_command_without_gh_pr_create_passes_through():
    cb = build_pr_base_branch_callback(policy={"foo/bar": "dev"})
    response = await cb(_input("git status"), None, {})
    assert response == {}


@pytest.mark.asyncio
async def test_repo_outside_policy_passes_through():
    cb = build_pr_base_branch_callback(policy={"schoolsoutapp/schools-out": "dev"})
    response = await cb(_input("gh pr create --repo other/repo --base main"), None, {})
    assert response == {}


@pytest.mark.asyncio
async def test_correct_base_passes():
    cb = build_pr_base_branch_callback(policy={"schoolsoutapp/schools-out": "dev"})
    response = await cb(
        _input("gh pr create --repo schoolsoutapp/schools-out --base dev --title x --body y"),
        None,
        {},
    )
    assert response == {}


@pytest.mark.asyncio
async def test_wrong_base_is_denied():
    cb = build_pr_base_branch_callback(policy={"schoolsoutapp/schools-out": "dev"})
    response = await cb(
        _input("gh pr create --repo schoolsoutapp/schools-out --base main --title x"),
        None,
        {},
    )
    decision = response["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "schoolsoutapp/schools-out" in decision["permissionDecisionReason"]
    assert "dev" in decision["permissionDecisionReason"]
    assert "main" in decision["permissionDecisionReason"]


@pytest.mark.asyncio
async def test_missing_base_is_denied_for_policy_repo():
    cb = build_pr_base_branch_callback(policy={"schoolsoutapp/schools-out": "dev"})
    response = await cb(
        _input("gh pr create --repo schoolsoutapp/schools-out --title x --body y"),
        None,
        {},
    )
    decision = response["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "--base" in decision["permissionDecisionReason"]


@pytest.mark.asyncio
async def test_chained_command_still_parsed():
    cb = build_pr_base_branch_callback(policy={"schoolsoutapp/schools-out": "dev"})
    response = await cb(
        _input("cd /tmp && git push && gh pr create --repo schoolsoutapp/schools-out --base main"),
        None,
        {},
    )
    decision = response["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"


@pytest.mark.asyncio
async def test_equals_form_base_flag():
    cb = build_pr_base_branch_callback(policy={"schoolsoutapp/schools-out": "dev"})
    bad = await cb(
        _input("gh pr create --repo=schoolsoutapp/schools-out --base=main"),
        None,
        {},
    )
    assert bad["hookSpecificOutput"]["permissionDecision"] == "deny"
    good = await cb(
        _input("gh pr create --repo=schoolsoutapp/schools-out --base=dev"),
        None,
        {},
    )
    assert good == {}


@pytest.mark.asyncio
async def test_policy_loaded_from_env_when_none(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(
        "SYMPHONY_PR_BASE_POLICY",
        '{"schoolsoutapp/schools-out": "dev"}',
    )
    cb = build_pr_base_branch_callback()
    response = await cb(
        _input("gh pr create --repo schoolsoutapp/schools-out --base main"),
        None,
        {},
    )
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.asyncio
async def test_env_invalid_json_is_treated_as_empty_policy(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SYMPHONY_PR_BASE_POLICY", "not-json")
    cb = build_pr_base_branch_callback()
    response = await cb(
        _input("gh pr create --repo schoolsoutapp/schools-out --base main"),
        None,
        {},
    )
    assert response == {}


@pytest.mark.asyncio
async def test_env_unset_with_no_explicit_policy(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SYMPHONY_PR_BASE_POLICY", raising=False)
    cb = build_pr_base_branch_callback()
    response = await cb(
        _input("gh pr create --repo schoolsoutapp/schools-out --base main"),
        None,
        {},
    )
    assert response == {}


@pytest.mark.asyncio
async def test_no_repo_flag_no_cwd_passes_through():
    """Without --repo and without cwd, hook can't determine target repo."""
    cb = build_pr_base_branch_callback(policy={"schoolsoutapp/schools-out": "dev"})
    response = await cb(_input("gh pr create --base main --title x"), None, {})
    assert response == {}


def _init_repo_with_origin(path: Path, origin_url: str) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", origin_url],
        check=True,
    )


@pytest.mark.asyncio
async def test_cwd_fallback_detects_repo_and_denies_wrong_base(tmp_path: Path):
    _init_repo_with_origin(tmp_path, "git@github.com:schoolsoutapp/schools-out.git")
    cb = build_pr_base_branch_callback(
        policy={"schoolsoutapp/schools-out": "dev"}, cwd=tmp_path
    )
    response = await cb(_input("gh pr create --base main --title x"), None, {})
    decision = response["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "schoolsoutapp/schools-out" in decision["permissionDecisionReason"]


@pytest.mark.asyncio
async def test_cwd_fallback_https_url_detected(tmp_path: Path):
    _init_repo_with_origin(tmp_path, "https://github.com/schoolsoutapp/schools-out.git")
    cb = build_pr_base_branch_callback(
        policy={"schoolsoutapp/schools-out": "dev"}, cwd=tmp_path
    )
    response = await cb(_input("gh pr create --title x"), None, {})
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.asyncio
async def test_cwd_fallback_correct_base_passes(tmp_path: Path):
    _init_repo_with_origin(tmp_path, "https://github.com/schoolsoutapp/schools-out.git")
    cb = build_pr_base_branch_callback(
        policy={"schoolsoutapp/schools-out": "dev"}, cwd=tmp_path
    )
    response = await cb(_input("gh pr create --base dev --title x"), None, {})
    assert response == {}


@pytest.mark.asyncio
async def test_cd_subdir_resolves_against_workspace_root(tmp_path: Path):
    """`cd fe-next-app && gh pr create` reads the subdir's git remote."""
    sub = tmp_path / "fe-next-app"
    sub.mkdir()
    _init_repo_with_origin(sub, "https://github.com/schoolsoutapp/fe-next-app.git")
    _init_repo_with_origin(tmp_path, "https://github.com/schoolsoutapp/schools-out.git")
    cb = build_pr_base_branch_callback(
        policy={
            "schoolsoutapp/schools-out": "dev",
            "schoolsoutapp/fe-next-app": "dev",
        },
        cwd=tmp_path,
    )
    response = await cb(
        _input("cd fe-next-app && gh pr create --base main --title x"), None, {}
    )
    decision = response["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "fe-next-app" in decision["permissionDecisionReason"]


@pytest.mark.asyncio
async def test_cwd_fallback_skips_non_github_origin(tmp_path: Path):
    _init_repo_with_origin(tmp_path, "git@gitlab.com:foo/bar.git")
    cb = build_pr_base_branch_callback(
        policy={"schoolsoutapp/schools-out": "dev"}, cwd=tmp_path
    )
    response = await cb(_input("gh pr create --base main --title x"), None, {})
    assert response == {}


@pytest.mark.asyncio
async def test_cwd_fallback_no_origin_remote_passes(tmp_path: Path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # noqa: ASYNC221 — short test setup
    cb = build_pr_base_branch_callback(
        policy={"schoolsoutapp/schools-out": "dev"}, cwd=tmp_path
    )
    response = await cb(_input("gh pr create --base main --title x"), None, {})
    assert response == {}
