"""Destructive-operation auto-policy for unattended agents.

Replaces devflow-lite's ``devflow-wizard`` skill, which gated destructive ops
behind a human confirmation prompt. An unattended agent has nobody to ask, so
the policy is decided up-front by the orchestrator.

Three strategies::

    Policy.AUTO_DENY      # safest — always block destructive Bash
    Policy.LOG_AND_ALLOW  # for trusted runs — allow but write a postmortem-able log
    Policy.ESCALATE       # block + emit marker the orchestrator can parse and re-dispatch

The matcher fires only for ``tool_name == "Bash"``. Patterns are conservative
(false negatives over false positives) — better to let a benign ``rm`` pass
than to silently delete a tree.
"""

from __future__ import annotations

import enum
import json
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HookInput = dict[str, Any]
HookOutput = dict[str, Any]
HookCallback = Callable[[HookInput, str | None, dict], Awaitable[HookOutput]]


class Policy(enum.Enum):
    AUTO_DENY = "auto_deny"
    LOG_AND_ALLOW = "log_and_allow"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class DestructivePattern:
    label: str
    regex: str


DESTRUCTIVE_PATTERNS: tuple[DestructivePattern, ...] = (
    DestructivePattern("rm -rf", r"\brm\s+(-[rRfF]+\s*)+"),
    DestructivePattern("git reset --hard", r"\bgit\s+reset\s+--hard\b"),
    DestructivePattern("git push --force", r"\bgit\s+push\s+(--force|-f)\b"),
    DestructivePattern("git clean -fd", r"\bgit\s+clean\s+(-[fdx]+\s*)+"),
    DestructivePattern("git branch -D", r"\bgit\s+branch\s+-D\b"),
    DestructivePattern("DROP TABLE", r"\bDROP\s+TABLE\b"),
    DestructivePattern("DROP DATABASE", r"\bDROP\s+DATABASE\b"),
    DestructivePattern("TRUNCATE", r"\bTRUNCATE\s+(TABLE\s+)?\w+"),
    DestructivePattern("dd to device", r"\bdd\s+.*\bof=/dev/"),
    DestructivePattern("mkfs", r"\bmkfs\.\w+\s+"),
)

_COMPILED = tuple((p, re.compile(p.regex, re.IGNORECASE)) for p in DESTRUCTIVE_PATTERNS)


def _match(command: str) -> DestructivePattern | None:
    for pattern, regex in _COMPILED:
        if regex.search(command):
            return pattern
    return None


def _log_destructive(state_root: Path, session_id: str, pattern: str, command: str) -> None:
    state_dir = Path(state_root) / "state" / session_id
    state_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": int(time.time()),
        "matched_pattern": pattern,
        "command": command,
        "pid": os.getpid(),
    }
    log = state_dir / "destructive.log"
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def build_policy_callback(policy: Policy, *, state_root: Path) -> HookCallback:
    state_root = Path(state_root).resolve()

    async def callback(input: HookInput, tool_use_id: str | None, context: dict) -> HookOutput:
        if input.get("tool_name") != "Bash":
            return {}
        command = (input.get("tool_input") or {}).get("command") or ""
        match = _match(command)
        if match is None:
            return {}

        session_id = input.get("session_id") or "default"
        if policy is Policy.AUTO_DENY:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        f"devflow_agent.policy: destructive command blocked "
                        f"({match.label}). Command: {command}"
                    ),
                }
            }
        if policy is Policy.LOG_AND_ALLOW:
            _log_destructive(state_root, session_id, match.label, command)
            return {}
        if policy is Policy.ESCALATE:
            return {
                "decision": "block",
                "reason": (
                    f"[devflow_agent:ESCALATE] destructive command intercepted "
                    f"({match.label}): {command}"
                ),
            }
        return {}

    return callback
