"""Programmatic replacement for devflow-lite's /spec UserPromptSubmit hook.

In Claude Code, the user types ``/spec <description>`` and
``spec_phase_tracker.py`` writes a PENDING marker. devflow-agent has no chat
input, so we materialise the marker straight from the issue context the
orchestrator is dispatching.

Marker schema (must match devflow-lite/hooks/spec_phase_tracker.py):

    {
      "status": "IMPLEMENTING" | "COMPLETED",
      "plan_path": "<identifier>: <title>",
      "started_at": <unix>,
      "cwd": "<absolute repo path>"
    }

We skip PENDING entirely — the agent always starts in IMPLEMENTING because the
plan *is* the issue body, the LLM is already executing.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class IssueContext:
    identifier: str
    title: str
    url: str


def _marker_path(state_root: Path, session_id: str) -> Path:
    return Path(state_root) / "state" / session_id / "active-spec.json"


def _atomic_write(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, target)


def mark_implementing(issue: IssueContext, *, state_root: Path, session_id: str, cwd: Path) -> Path:
    """Write the IMPLEMENTING marker for ``session_id``. Returns the marker path."""
    target = _marker_path(state_root, session_id)
    plan = f"{issue.identifier}: {issue.title}"
    payload = {
        "status": "IMPLEMENTING",
        "plan_path": plan,
        "started_at": int(time.time()),
        "cwd": str(cwd),
    }
    _atomic_write(target, json.dumps(payload))
    return target


def mark_completed(*, state_root: Path, session_id: str) -> Path:
    """Flip the marker to COMPLETED. No-op (returns target) if absent."""
    target = _marker_path(state_root, session_id)
    if not target.exists():
        return target
    try:
        data = json.loads(target.read_text())
    except (json.JSONDecodeError, OSError):
        return target
    data["status"] = "COMPLETED"
    _atomic_write(target, json.dumps(data))
    return target
