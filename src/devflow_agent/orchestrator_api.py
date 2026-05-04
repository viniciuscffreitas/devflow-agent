"""Orchestrator-side API: inspect or terminate a running devflow-agent.

Symphony (or any caller) keeps the agent's ``session_id`` and ``state_root``
that ``build_options`` returned. With those two it can:

- ``read_spec_status``: poll where the spec is (IMPLEMENTING / COMPLETED / ABORTED).
- ``abort_spec``: write ``ABORTED`` with a reason. The next Stop will pass
  spec_stop_guard cleanly because ABORTED ≠ PENDING/IMPLEMENTING.
- ``read_destructive_log``: post-mortem of every destructive Bash the policy
  hook intercepted (only populated under ``Policy.LOG_AND_ALLOW``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _marker_path(state_root: Path, session_id: str) -> Path:
    return Path(state_root) / "state" / session_id / "active-spec.json"


def _atomic_write(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, target)


def read_spec_status(*, state_root: Path, session_id: str) -> str | None:
    target = _marker_path(state_root, session_id)
    if not target.exists():
        return None
    try:
        data = json.loads(target.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    status = data.get("status")
    return status if isinstance(status, str) else None


def abort_spec(*, state_root: Path, session_id: str, reason: str) -> None:
    target = _marker_path(state_root, session_id)
    if not target.exists():
        return
    try:
        data = json.loads(target.read_text())
    except (json.JSONDecodeError, OSError):
        data = {}
    data["status"] = "ABORTED"
    data["abort_reason"] = reason
    _atomic_write(target, json.dumps(data))


def read_destructive_log(*, state_root: Path, session_id: str) -> list[dict[str, Any]]:
    log = Path(state_root) / "state" / session_id / "destructive.log"
    if not log.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
