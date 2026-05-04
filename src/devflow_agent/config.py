"""High-level entry point: build a wired ClaudeAgentOptions for an unattended run.

Caller (Symphony orchestrator, CI runner, anything driving claude-agent-sdk
without a human) does::

    options, session_id = await build_options(
        issue=IssueContext(identifier="SODEV-789", title="...", url="..."),
        cwd=Path("/path/to/repo-clone"),
        devflow_root=Path("/path/to/devflow-lite"),
        policy=Policy.AUTO_DENY,
        base_system_prompt="...",
    )
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt="...")

What this wires:

1. Generates a fresh ``session_id`` (UUID4) — devflow hooks key state on it.
2. Writes the ``IMPLEMENTING`` marker via spec_seed.
3. Runs SessionStart-equivalent bootstrap hooks and folds their context into
   ``system_prompt``.
4. Builds ``HookMatcher`` lists for PreToolUse / PostToolUse / Stop / PreCompact
   pointing at the real lite hooks (skipping UserPromptSubmit — the agent has
   no user prompt) and the destructive-op policy callback.

What this does NOT do:

- Set ``permission_mode``. Caller picks (``"acceptEdits"``,
  ``"bypassPermissions"``, ...) based on their trust level for the run.
- Provide tools or MCP servers. Caller adds those.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from devflow_agent.bootstrap import DEFAULT_BOOTSTRAP_HOOKS, collect_session_context
from devflow_agent.bridge import build_hook_callback
from devflow_agent.policy import Policy, build_policy_callback
from devflow_agent.spec_seed import IssueContext, mark_implementing

_PRE_TOOL_USE_WRITE_EDIT = ("secrets_gate",)
_PRE_TOOL_USE_BASH = ("pre_push_gate", "commit_validator")
_POST_TOOL_USE_WRITE_EDIT = ("file_checker", "tdd_enforcer", "pre_edit_overwrite_guard")
_POST_TOOL_USE_ANY = ("context_monitor", "concurrent_edit_lock", "codeowners_check")
_STOP_HOOKS = ("stop_dispatcher",)
_PRE_COMPACT_HOOKS = ("pre_compact",)


def _hook_matcher(matcher: str, callback: Any, timeout: float | None = 60) -> Any:
    """Lazy import HookMatcher so importing devflow_agent.config doesn't require SDK."""
    from claude_agent_sdk import HookMatcher

    return HookMatcher(matcher=matcher, hooks=[callback], timeout=timeout)


async def build_options(
    *,
    issue: IssueContext,
    cwd: Path,
    devflow_root: Path,
    policy: Policy,
    base_system_prompt: str,
    bootstrap_hooks: tuple[str, ...] = DEFAULT_BOOTSTRAP_HOOKS,
) -> tuple[Any, str]:
    """Return ``(ClaudeAgentOptions, session_id)`` for an unattended run."""
    from claude_agent_sdk import ClaudeAgentOptions

    cwd = Path(cwd).resolve()
    devflow_root = Path(devflow_root).resolve()
    hooks_dir = devflow_root / "hooks"
    state_root = devflow_root

    session_id = str(uuid.uuid4())

    mark_implementing(issue, state_root=state_root, session_id=session_id, cwd=cwd)

    contexts = await collect_session_context(
        bootstrap_hooks,
        hooks_dir=hooks_dir,
        devflow_root=devflow_root,
        session_id=session_id,
        cwd=cwd,
    )
    system_prompt: str | None
    if base_system_prompt and contexts:
        system_prompt = base_system_prompt + "\n\n" + "\n\n".join(contexts)
    elif base_system_prompt:
        system_prompt = base_system_prompt
    elif contexts:
        system_prompt = "\n\n".join(contexts)
    else:
        system_prompt = None

    def _bridge(scripts: tuple[str, ...]) -> Any:
        return build_hook_callback(scripts, hooks_dir=hooks_dir, devflow_root=devflow_root)

    policy_cb = build_policy_callback(policy, state_root=state_root)

    hooks = {
        "PreToolUse": [
            _hook_matcher("Bash", policy_cb),
            _hook_matcher("Write|Edit|MultiEdit", _bridge(_PRE_TOOL_USE_WRITE_EDIT)),
            _hook_matcher("Bash", _bridge(_PRE_TOOL_USE_BASH)),
        ],
        "PostToolUse": [
            _hook_matcher("Write|Edit|MultiEdit", _bridge(_POST_TOOL_USE_WRITE_EDIT)),
            _hook_matcher(".*", _bridge(_POST_TOOL_USE_ANY)),
        ],
        "Stop": [_hook_matcher("", _bridge(_STOP_HOOKS))],
        "PreCompact": [_hook_matcher("", _bridge(_PRE_COMPACT_HOOKS))],
    }

    options = ClaudeAgentOptions(
        hooks=hooks,
        system_prompt=system_prompt,
        cwd=str(cwd),
    )
    return options, session_id
