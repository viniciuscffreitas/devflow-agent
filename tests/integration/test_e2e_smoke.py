"""End-to-end smoke: devflow-agent wires real devflow-lite hooks.

Skipped unless ``DEVFLOW_LITE_PATH`` points at a local checkout of the
post-Phase-1 lite repo.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from devflow_agent.config import build_options
from devflow_agent.policy import Policy
from devflow_agent.spec_seed import IssueContext

LITE = os.environ.get("DEVFLOW_LITE_PATH")
pytestmark = pytest.mark.skipif(
    LITE is None or not Path(LITE).exists(),
    reason="set DEVFLOW_LITE_PATH to a checked-out devflow-lite repo to run e2e",
)


@pytest.mark.asyncio
async def test_build_options_against_real_lite(tmp_path: Path):
    devflow_root = Path(LITE)
    issue = IssueContext(identifier="E2E-1", title="smoke", url="")
    options, session_id = await build_options(
        issue=issue,
        cwd=tmp_path,
        devflow_root=devflow_root,
        policy=Policy.AUTO_DENY,
        base_system_prompt="System prompt under test.",
    )

    marker = devflow_root / "state" / session_id / "active-spec.json"
    try:
        assert marker.exists()
        data = json.loads(marker.read_text())
        assert data["status"] == "IMPLEMENTING"
        assert data["plan_path"] == "E2E-1: smoke"
    finally:
        if marker.exists():
            marker.unlink()
        try:
            marker.parent.rmdir()
        except OSError:
            pass

    assert "System prompt under test." in options.system_prompt
    assert len(options.hooks["PreToolUse"]) >= 1
    assert len(options.hooks["Stop"]) == 1
