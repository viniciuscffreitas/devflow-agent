"""Orchestrator-side API for inspecting and controlling an agent run."""

from __future__ import annotations

import json
from pathlib import Path

from devflow_agent.orchestrator_api import (
    abort_spec,
    read_destructive_log,
    read_spec_status,
)


def _write_marker(state_root: Path, sid: str, status: str) -> Path:
    target = state_root / "state" / sid / "active-spec.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "status": status,
                "plan_path": "T-1: x",
                "started_at": 1,
                "cwd": str(state_root),
            }
        )
    )
    return target


def test_read_spec_status_returns_status(tmp_path: Path):
    _write_marker(tmp_path, "s1", "IMPLEMENTING")
    assert read_spec_status(state_root=tmp_path, session_id="s1") == "IMPLEMENTING"


def test_read_spec_status_returns_none_when_absent(tmp_path: Path):
    assert read_spec_status(state_root=tmp_path, session_id="missing") is None


def test_abort_spec_writes_aborted_status(tmp_path: Path):
    _write_marker(tmp_path, "s2", "IMPLEMENTING")
    abort_spec(state_root=tmp_path, session_id="s2", reason="orchestrator killed")
    data = json.loads((tmp_path / "state" / "s2" / "active-spec.json").read_text())
    assert data["status"] == "ABORTED"
    assert data["abort_reason"] == "orchestrator killed"


def test_abort_spec_is_idempotent(tmp_path: Path):
    _write_marker(tmp_path, "s3", "IMPLEMENTING")
    abort_spec(state_root=tmp_path, session_id="s3", reason="r1")
    abort_spec(state_root=tmp_path, session_id="s3", reason="r2")
    data = json.loads((tmp_path / "state" / "s3" / "active-spec.json").read_text())
    assert data["abort_reason"] == "r2"


def test_read_destructive_log_returns_records(tmp_path: Path):
    log_dir = tmp_path / "state" / "s4"
    log_dir.mkdir(parents=True)
    (log_dir / "destructive.log").write_text(
        json.dumps({"ts": 1, "matched_pattern": "rm -rf", "command": "rm -rf /tmp/x", "pid": 1})
        + "\n"
        + json.dumps(
            {
                "ts": 2,
                "matched_pattern": "git push --force",
                "command": "git push --force",
                "pid": 2,
            }
        )
        + "\n"
    )
    records = read_destructive_log(state_root=tmp_path, session_id="s4")
    assert len(records) == 2
    assert records[0]["matched_pattern"] == "rm -rf"


def test_read_destructive_log_empty_when_missing(tmp_path: Path):
    assert read_destructive_log(state_root=tmp_path, session_id="none") == []
