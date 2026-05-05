"""High-level entry point: build a wired ClaudeAgentOptions for an unattended run.

Two public entry points:

- ``compose_devflow_bundle`` — for callers that already build their own
  ``ClaudeAgentOptions`` (Symphony shim, custom CI runners) and just want the
  session_id, merged system_prompt and ready-to-attach hooks dict.
- ``build_options`` — convenience wrapper that constructs a minimal
  ``ClaudeAgentOptions`` for callers without their own SDK plumbing.

Caller picks ``permission_mode``, MCP servers, env, disallowed_tools — those
are caller-specific and not the harness's concern.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from devflow_agent.bootstrap import DEFAULT_BOOTSTRAP_HOOKS, collect_session_context
from devflow_agent.bridge import build_hook_callback
from devflow_agent.ci_gate_bootstrap import build_ci_gate_callback, sync_ci_gate
from devflow_agent.policy import Policy, build_policy_callback
from devflow_agent.pr_base_branch import build_pr_base_branch_callback
from devflow_agent.spec_seed import IssueContext, mark_implementing

_PRE_TOOL_USE_WRITE_EDIT = ("secrets_gate",)
_PRE_TOOL_USE_BASH = ("pre_push_gate", "commit_validator")
_POST_TOOL_USE_WRITE_EDIT = ("file_checker", "tdd_enforcer", "pre_edit_overwrite_guard")
_POST_TOOL_USE_ANY = ("context_monitor", "concurrent_edit_lock", "codeowners_check")
_STOP_HOOKS = ("stop_dispatcher",)
_PRE_COMPACT_HOOKS = ("pre_compact",)


@dataclass(frozen=True)
class DevflowBundle:
    """Pre-wired devflow pieces ready to merge into caller's ClaudeAgentOptions."""

    session_id: str
    system_prompt: str | None
    hooks: dict[str, list[Any]]


def _hook_matcher(matcher: str, callback: Any, timeout: float | None = 60) -> Any:
    """Lazy import HookMatcher so importing devflow_agent.config doesn't require SDK."""
    from claude_agent_sdk import HookMatcher

    return HookMatcher(matcher=matcher, hooks=[callback], timeout=timeout)


async def compose_devflow_bundle(
    *,
    issue: IssueContext,
    cwd: Path,
    devflow_root: Path,
    policy: Policy,
    base_system_prompt: str = "",
    bootstrap_hooks: tuple[str, ...] = DEFAULT_BOOTSTRAP_HOOKS,
) -> DevflowBundle:
    """Materialise the IMPLEMENTING marker, run bootstrap hooks, build hook table.

    Returns a :class:`DevflowBundle` the caller plugs into its own
    :class:`ClaudeAgentOptions`. ``state_root`` always equals ``devflow_root``
    — devflow-lite hooks resolve state under ``DEVFLOW_ROOT/state/``.
    """
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

    sync_ci_gate(cwd)

    policy_cb = build_policy_callback(policy, state_root=state_root)
    pr_base_cb = build_pr_base_branch_callback(cwd=cwd)
    ci_gate_cb = build_ci_gate_callback(cwd=cwd)

    hooks = {
        "PreToolUse": [
            _hook_matcher("Bash", policy_cb),
            _hook_matcher("Bash", pr_base_cb),
            _hook_matcher("Bash", ci_gate_cb),
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

    return DevflowBundle(session_id=session_id, system_prompt=system_prompt, hooks=hooks)


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

    bundle = await compose_devflow_bundle(
        issue=issue,
        cwd=cwd,
        devflow_root=devflow_root,
        policy=policy,
        base_system_prompt=base_system_prompt,
        bootstrap_hooks=bootstrap_hooks,
    )
    options = ClaudeAgentOptions(
        hooks=bundle.hooks,
        system_prompt=bundle.system_prompt,
        cwd=str(Path(cwd).resolve()),
    )
    return options, bundle.session_id
