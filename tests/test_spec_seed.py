"""spec_seed creates devflow-lite's active-spec.json marker programmatically."""

from __future__ import annotations

import json
from pathlib import Path

from devflow_agent.spec_seed import IssueContext, mark_completed, mark_implementing


def test_mark_implementing_writes_marker(tmp_path: Path):
    issue = IssueContext(
        identifier="SODEV-789",
        title="add robots.txt",
        url="https://linear.app/x/SODEV-789",
    )
    target = mark_implementing(
        issue,
        state_root=tmp_path,
        session_id="sess-1",
        cwd=tmp_path / "repo",
    )
    assert target.exists()
    data = json.loads(target.read_text())
    assert data["status"] == "IMPLEMENTING"
    assert data["plan_path"] == "SODEV-789: add robots.txt"
    assert data["cwd"] == str(tmp_path / "repo")
    assert isinstance(data["started_at"], int)


def test_mark_completed_overwrites_status(tmp_path: Path):
    issue = IssueContext(identifier="X-1", title="t", url="")
    mark_implementing(issue, state_root=tmp_path, session_id="s", cwd=tmp_path)
    target = mark_completed(state_root=tmp_path, session_id="s")
    data = json.loads(target.read_text())
    assert data["status"] == "COMPLETED"
    assert data["plan_path"] == "X-1: t"


def test_mark_completed_is_noop_when_marker_absent(tmp_path: Path):
    mark_completed(state_root=tmp_path, session_id="missing")
