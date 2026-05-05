"""Tests for ci_gate_bootstrap: sync_ci_gate + build_ci_gate_callback."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from devflow_agent.ci_gate_bootstrap import build_ci_gate_callback, sync_ci_gate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SIMPLE_LINT_WORKFLOW = """\
name: CI
on: [push, pull_request]
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install deps
        run: npm ci
      - name: Lint
        run: npm run lint
      - name: Type check
        run: npx tsc --noEmit
"""

RUBY_WORKFLOW = """\
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Bundle install
        run: bundle install
      - name: RuboCop
        run: bundle exec rubocop
      - name: RSpec
        run: bundle exec rspec
"""

WORKFLOW_WITH_DEPLOY = """\
name: CI
on: [push]
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npm run lint
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npm run build
      - run: vercel deploy --prod
"""

WORKFLOW_WITH_SERVICES = """\
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16
    steps:
      - uses: actions/checkout@v4
      - run: bundle exec rspec
"""

WORKFLOW_WITH_SECRETS = """\
name: CI
on: [push]
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npm run lint
      - name: Deploy
        run: curl -H "Authorization: ${{ secrets.API_TOKEN }}" https://example.com/deploy
"""

WORKFLOW_DOCKER = """\
name: CI
on: [push]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: docker build -t myapp .
      - run: docker push myapp
"""

MULTI_STEP_WORKFLOW = """\
name: CI
on: [push]
jobs:
  quality:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: |
          npm ci
          npm run format:check
          npm run lint
"""


def _write_workflow(workflows_dir: Path, name: str, content: str) -> None:
    workflows_dir.mkdir(parents=True, exist_ok=True)
    (workflows_dir / name).write_text(content)


def _hook_input(cmd: str) -> dict:
    return {
        "session_id": "test-session",
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "hook_event_name": "PreToolUse",
    }


# ---------------------------------------------------------------------------
# sync_ci_gate
# ---------------------------------------------------------------------------


def test_no_workflows_dir_returns_none(tmp_path: Path) -> None:
    result = sync_ci_gate(tmp_path)
    assert result is None
    assert not (tmp_path / ".devflow" / "ci-commands.sh").exists()


def test_empty_workflows_dir_returns_none(tmp_path: Path) -> None:
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    result = sync_ci_gate(tmp_path)
    assert result is None


def test_extracts_lint_commands_from_simple_workflow(tmp_path: Path) -> None:
    _write_workflow(tmp_path / ".github" / "workflows", "ci.yml", SIMPLE_LINT_WORKFLOW)
    result = sync_ci_gate(tmp_path)
    assert result is not None
    content = result.read_text()
    assert "npm ci" in content
    assert "npm run lint" in content
    assert "npx tsc --noEmit" in content


def test_extracts_ruby_commands(tmp_path: Path) -> None:
    _write_workflow(tmp_path / ".github" / "workflows", "ci.yml", RUBY_WORKFLOW)
    result = sync_ci_gate(tmp_path)
    assert result is not None
    content = result.read_text()
    assert "bundle install" in content
    assert "bundle exec rubocop" in content
    assert "bundle exec rspec" in content


def test_skips_deploy_job(tmp_path: Path) -> None:
    _write_workflow(tmp_path / ".github" / "workflows", "ci.yml", WORKFLOW_WITH_DEPLOY)
    result = sync_ci_gate(tmp_path)
    assert result is not None
    content = result.read_text()
    assert "npm run lint" in content
    assert "vercel deploy" not in content


def test_skips_job_with_services(tmp_path: Path) -> None:
    _write_workflow(tmp_path / ".github" / "workflows", "ci.yml", WORKFLOW_WITH_SERVICES)
    result = sync_ci_gate(tmp_path)
    # No portable commands extracted → None
    assert result is None


def test_skips_step_referencing_secrets(tmp_path: Path) -> None:
    _write_workflow(tmp_path / ".github" / "workflows", "ci.yml", WORKFLOW_WITH_SECRETS)
    result = sync_ci_gate(tmp_path)
    assert result is not None
    content = result.read_text()
    assert "npm run lint" in content
    assert "secrets.API_TOKEN" not in content
    assert "curl" not in content


def test_skips_docker_commands(tmp_path: Path) -> None:
    _write_workflow(tmp_path / ".github" / "workflows", "ci.yml", WORKFLOW_DOCKER)
    result = sync_ci_gate(tmp_path)
    assert result is None


def test_multi_step_block_extracted(tmp_path: Path) -> None:
    _write_workflow(tmp_path / ".github" / "workflows", "ci.yml", MULTI_STEP_WORKFLOW)
    result = sync_ci_gate(tmp_path)
    assert result is not None
    content = result.read_text()
    assert "npm ci" in content
    assert "npm run format:check" in content
    assert "npm run lint" in content


def test_output_is_executable_shell_script(tmp_path: Path) -> None:
    _write_workflow(tmp_path / ".github" / "workflows", "ci.yml", SIMPLE_LINT_WORKFLOW)
    result = sync_ci_gate(tmp_path)
    assert result is not None
    assert result.suffix == ".sh"
    assert result.stat().st_mode & stat.S_IXUSR


def test_script_starts_with_set_e(tmp_path: Path) -> None:
    _write_workflow(tmp_path / ".github" / "workflows", "ci.yml", RUBY_WORKFLOW)
    result = sync_ci_gate(tmp_path)
    assert result is not None
    assert result.read_text().startswith("#!/usr/bin/env bash\nset -euo pipefail")


def test_rerun_overwrites_existing_file(tmp_path: Path) -> None:
    wf_dir = tmp_path / ".github" / "workflows"
    _write_workflow(wf_dir, "ci.yml", SIMPLE_LINT_WORKFLOW)
    first = sync_ci_gate(tmp_path)
    assert first is not None

    # Replace workflow with ruby one
    (wf_dir / "ci.yml").write_text(RUBY_WORKFLOW)
    second = sync_ci_gate(tmp_path)
    assert second is not None
    content = second.read_text()
    # Old npm commands gone, ruby commands present
    assert "npm run lint" not in content
    assert "bundle exec rubocop" in content


def test_merges_multiple_workflow_files(tmp_path: Path) -> None:
    wf_dir = tmp_path / ".github" / "workflows"
    _write_workflow(wf_dir, "frontend.yml", SIMPLE_LINT_WORKFLOW)
    _write_workflow(wf_dir, "backend.yml", RUBY_WORKFLOW)
    result = sync_ci_gate(tmp_path)
    assert result is not None
    content = result.read_text()
    assert "npm run lint" in content
    assert "bundle exec rubocop" in content


def test_written_to_devflow_subdir(tmp_path: Path) -> None:
    _write_workflow(tmp_path / ".github" / "workflows", "ci.yml", SIMPLE_LINT_WORKFLOW)
    result = sync_ci_gate(tmp_path)
    assert result is not None
    assert result == tmp_path / ".devflow" / "ci-commands.sh"


# ---------------------------------------------------------------------------
# build_ci_gate_callback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_bash_passes_through(tmp_path: Path) -> None:
    cb = build_ci_gate_callback(cwd=tmp_path)
    response = await cb(
        {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/x"}}, None, {}
    )
    assert response == {}


@pytest.mark.asyncio
async def test_non_push_bash_passes_through(tmp_path: Path) -> None:
    cb = build_ci_gate_callback(cwd=tmp_path)
    response = await cb(_hook_input("git status"), None, {})
    assert response == {}


@pytest.mark.asyncio
async def test_push_without_ci_commands_passes_through(tmp_path: Path) -> None:
    cb = build_ci_gate_callback(cwd=tmp_path)
    response = await cb(_hook_input("git push origin main"), None, {})
    assert response == {}


@pytest.mark.asyncio
async def test_push_with_passing_ci_commands_allows(tmp_path: Path) -> None:
    ci_script = tmp_path / ".devflow" / "ci-commands.sh"
    ci_script.parent.mkdir(parents=True)
    ci_script.write_text("#!/usr/bin/env bash\nset -euo pipefail\necho 'lint ok'\n")
    ci_script.chmod(0o755)

    cb = build_ci_gate_callback(cwd=tmp_path)
    response = await cb(_hook_input("git push origin dev"), None, {})
    assert response == {}


@pytest.mark.asyncio
async def test_push_with_failing_ci_commands_denies(tmp_path: Path) -> None:
    ci_script = tmp_path / ".devflow" / "ci-commands.sh"
    ci_script.parent.mkdir(parents=True)
    ci_script.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\necho 'lint failed' >&2\nexit 1\n"
    )
    ci_script.chmod(0o755)

    cb = build_ci_gate_callback(cwd=tmp_path)
    response = await cb(_hook_input("git push"), None, {})
    decision = response["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "lint failed" in decision["permissionDecisionReason"]


@pytest.mark.asyncio
async def test_deny_message_includes_ci_gate_prefix(tmp_path: Path) -> None:
    ci_script = tmp_path / ".devflow" / "ci-commands.sh"
    ci_script.parent.mkdir(parents=True)
    ci_script.write_text("#!/usr/bin/env bash\nset -euo pipefail\nexit 1\n")
    ci_script.chmod(0o755)

    cb = build_ci_gate_callback(cwd=tmp_path)
    response = await cb(_hook_input("git push --force-with-lease"), None, {})
    reason = response["hookSpecificOutput"]["permissionDecisionReason"]
    assert "ci_gate" in reason


@pytest.mark.asyncio
async def test_push_force_also_gated(tmp_path: Path) -> None:
    ci_script = tmp_path / ".devflow" / "ci-commands.sh"
    ci_script.parent.mkdir(parents=True)
    ci_script.write_text("#!/usr/bin/env bash\nexit 1\n")
    ci_script.chmod(0o755)

    cb = build_ci_gate_callback(cwd=tmp_path)
    response = await cb(_hook_input("git push --force origin dev"), None, {})
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.asyncio
async def test_no_cwd_push_passes_through() -> None:
    cb = build_ci_gate_callback()
    response = await cb(_hook_input("git push"), None, {})
    assert response == {}
