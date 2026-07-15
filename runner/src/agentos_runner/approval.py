"""Approval gates in the runner: the policy-gate tool and the permission gate.

ADR-0010 (approval gates, epic #22) defines one approval primitive with two
trigger types, and this module is the runner half of both:

- **Policy gate** (#244): the agent's own logic decides something needs a
  human decision. An in-process SDK MCP tool
  (``mcp__agentos__request_approval``) is carried by every session, so a
  skill instructs the model to call it with a one-line summary. The call
  executes no real-world action; it only marks the turn, and the session
  emits its terminal ``final`` with ``status=awaiting-approval``.
- **Permission gate** (#245): configuration marks a tool as
  approval-required, and the runner intercepts the model-initiated call
  proactively through the SDK ``can_use_tool`` callback -- the replacement
  for the previously hardcoded ``permission_mode="bypassPermissions"``. The
  gated call is denied (never executed), the ``ApprovalGate`` records what
  was blocked, and the turn ends ``awaiting-approval`` exactly like a policy
  gate, so the durable-record/suspend/resume lifecycle (#244) is shared.

The durable ``Approval`` record, the authorizer, and the resume trigger all
live server-side with the worker/API (they must not be spoofable from inside
the sandbox -- see docs/interfaces/approval/INTERFACE.md); the runner's only
job is to end the turn in the awaiting-approval state.

Policy-gate detection is wire-level, not execution-level: ``translate.py``
captures the ``ToolUseBlock`` naming the request tool, so the fake-model path
(a scripted ``ToolUseBlock``, no tool execution, no network) exercises the
identical seam as a real model call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import (
    CanUseTool,
    McpSdkServerConfig,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)
from plugin_format import ApprovalPolicy, PluginManifest

# The server key under ClaudeAgentOptions.mcp_servers; the SDK prefixes tool
# names as mcp__<server>__<tool>.
APPROVAL_SERVER_NAME = "agentos"
_TOOL_NAME = "request_approval"
# The fully qualified tool identifier as it appears on ToolUseBlock.name.
APPROVAL_TOOL_NAME = f"mcp__{APPROVAL_SERVER_NAME}__{_TOOL_NAME}"

_TOOL_DESCRIPTION = (
    "Request human approval before proceeding. Call this when your"
    " instructions say a step needs sign-off (a discount, an invoice, a"
    " remediation). Pass a one-line summary of exactly what needs approval,"
    " and, when your instructions name an approval route for this kind of"
    " decision, pass it as route (the platform delivers the request to that"
    " route's channel). After calling it, end your turn and tell the user the"
    " request is pending; the platform pauses the session and resumes it with"
    " the decision once an authorized human resolves it."
)

# Full JSON schema (not the shorthand type map) so ``route`` is optional: a
# request without a route falls back to the requesting channel.
_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "One line stating exactly what needs approval.",
        },
        "route": {
            "type": "string",
            "description": "Optional approval route name from your instructions.",
        },
    },
    "required": ["summary"],
}


@tool(_TOOL_NAME, _TOOL_DESCRIPTION, _TOOL_SCHEMA)
async def _request_approval(args: dict[str, Any]) -> dict[str, Any]:
    summary = str(args.get("summary") or "").strip()
    if not summary:
        return {
            "content": [
                {
                    "type": "text",
                    "text": "Rejected: pass a non-empty summary of what needs approval.",
                }
            ],
            "is_error": True,
        }
    return {
        "content": [
            {
                "type": "text",
                "text": (
                    "Approval requested. The session will pause awaiting a human"
                    " decision; end your turn now and tell the user the request"
                    " is pending."
                ),
            }
        ]
    }


def build_approval_server() -> McpSdkServerConfig:
    """The in-process MCP server carrying the approval-request tool."""

    return create_sdk_mcp_server(
        name=APPROVAL_SERVER_NAME, version="1.0.0", tools=[_request_approval]
    )


# --- The permission gate (#245): canUseTool over approval-required tools --------

# Longest tool-input rendering carried into an approval summary. The summary is
# shown to a human approver and persisted on the Approval record; a multi-KB
# tool input would drown both, so it is truncated with an ellipsis marker.
_SUMMARY_INPUT_LIMIT = 300

# The permission-gate summary prefix, in one place so the worker can pin against
# it (#430, ADR-0035): the worker treats a persisted approval summary starting
# with this prefix as a permission-gate block eligible for the one-shot grant,
# and reads the approved tool name from it. Keep summarize_tool_call's output
# identical to today -- this only names the shared literal.
#
# This prefix is a RESERVED namespace owned by ``summarize_tool_call`` -- the
# runner is the single writer of a genuine ``can_use_tool`` denial summary. Only
# machine-generated permission-gate summaries may live in it. Model- and
# skill-authored (policy-gate) summaries come from the model's own
# ``request_approval(summary=...)`` argument and are guarded out of this
# namespace by ``guard_reserved_summary`` before they are stored, so the worker's
# "summary starts with the prefix" check is an authoritative provenance signal: a
# prompt-injected agent cannot forge a real permission-gate block by naming a tool
# in its summary and thereby mint an unreviewed one-shot bypass grant (#430,
# ADR-0035, security).
APPROVAL_SUMMARY_PREFIX = "Tool call awaiting approval: "

# Prepended to a model/skill-authored summary that would otherwise collide with
# the reserved permission-gate namespace, neutralizing the forgery while leaving
# the human-facing text readable.
_RESERVED_SUMMARY_MARKER = "[agent-requested] "


def guard_reserved_summary(summary: str) -> str:
    """Keep a policy-gate summary out of the reserved permission-gate namespace.

    ``APPROVAL_SUMMARY_PREFIX`` is reserved for machine-generated permission-gate
    summaries (``summarize_tool_call``), which the worker trusts as proof that a
    real ``can_use_tool`` denial occurred before minting a one-shot bypass grant.
    A model/skill-authored summary is attacker-influenced, so if it starts with
    the reserved prefix this prepends a neutralizing marker so the result no
    longer does; any other summary is returned unchanged (#430, ADR-0035).
    """

    if summary.startswith(APPROVAL_SUMMARY_PREFIX):
        return f"{_RESERVED_SUMMARY_MARKER}{summary}"
    return summary

# What the denied model is told. It must steer the model to end the turn (so
# the session can emit the awaiting-approval final and the platform can
# suspend), and to not spin on retries -- a retried call is denied identically.
_DENY_MESSAGE = (
    "This tool call requires human approval and was NOT executed. An approval"
    " request has been recorded; the session will pause for a human decision."
    " Do not retry the tool. End your turn now and tell the user exactly what"
    " is pending approval."
)


def summarize_tool_call(tool_name: str, tool_input: dict[str, Any]) -> str:
    """A one-line, human-readable statement of the blocked call.

    This becomes the ``approval_summary`` on the awaiting-approval final and
    therefore the durable record's summary a human resolves against, so it
    names the tool and a compact rendering of its input.
    """

    try:
        rendered = json.dumps(tool_input, sort_keys=True, default=str)
    except (TypeError, ValueError):
        rendered = str(tool_input)
    if len(rendered) > _SUMMARY_INPUT_LIMIT:
        rendered = rendered[:_SUMMARY_INPUT_LIMIT] + "... (truncated)"
    return f"{APPROVAL_SUMMARY_PREFIX}{tool_name} {rendered}"


@dataclass
class ApprovalGate:
    """Shared state between the ``can_use_tool`` callback and the session loop.

    ``required`` is the per-agent set of approval-required tool names: the
    union of the bundle manifest's ``approvalPolicy`` gates (#247, versioned
    with the agent) and the AGENTOS_APPROVAL_REQUIRED_TOOLS env override.
    ``route_by_tool`` maps a manifest-gated tool to its declared route name,
    so a blocked call carries the route the platform binds to a channel.
    ``pending_summary``/``pending_route`` are set by the callback when it
    blocks a call and consumed by the session at turn end to flip the final
    to awaiting-approval; ``reset()`` clears them at each turn start so one
    turn's block never leaks into the next.

    The first blocked call of a turn wins: a model that retries the denied
    tool (against the deny message's instruction) does not overwrite the
    summary the human will resolve against.

    ``grant_tool`` is the one-shot post-approval allowance (#430, ADR-0035): the
    single tool name the worker injects from durable state at resume boot so a
    genuinely-approved permission-gate call completes exactly once.
    ``consume_grant`` spends it (one use, tool-name-scoped), and ``reset()``
    expires any unspent grant after the boot turn. The grant is deliberately
    boot-turn-only so it never leaks across turns: ``reset()`` runs at the start
    of every turn, so the FIRST reset (the boot turn) preserves a freshly
    injected grant and every later reset clears it.
    """

    required: frozenset[str] = field(default_factory=frozenset)
    route_by_tool: dict[str, str] = field(default_factory=dict)
    pending_summary: str | None = None
    pending_route: str | None = None
    grant_tool: str | None = None
    _boot_turn_seen: bool = False

    def reset(self) -> None:
        self.pending_summary = None
        self.pending_route = None
        # Boot-turn-only grant: keep it on the first reset (the boot turn),
        # expire any unspent grant on the second and later resets so it never
        # leaks into a subsequent turn.
        if self._boot_turn_seen:
            self.grant_tool = None
        self._boot_turn_seen = True

    def consume_grant(self, tool_name: str) -> bool:
        """Spend the one-shot grant iff it names ``tool_name`` (single use)."""

        if self.grant_tool is not None and tool_name == self.grant_tool:
            self.grant_tool = None
            return True
        return False

    def block(self, tool_name: str, tool_input: dict[str, Any]) -> None:
        if self.pending_summary is None:
            self.pending_summary = summarize_tool_call(tool_name, tool_input)
            self.pending_route = self.route_by_tool.get(tool_name)


def build_can_use_tool(gate: ApprovalGate) -> CanUseTool:
    """The SDK permission callback replacing the hardcoded bypass (#245).

    Approval-required tools are denied (the call never executes) and recorded
    on the gate; every other tool is allowed, preserving the pre-gate posture
    for unconfigured tools. The decision is proactive -- the call is blocked
    before execution -- unlike the reactive ``side_effect_flag`` classifier,
    which only reports after the fact.
    """

    async def can_use_tool(
        tool_name: str,
        tool_input: dict[str, Any],
        _context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        if tool_name in gate.required:
            # The one-shot post-approval allowance (#430): a resume-boot grant
            # for exactly this tool lets one call through (no block recorded, the
            # approved action completes) and re-arms the gate.
            if gate.consume_grant(tool_name):
                return PermissionResultAllow()
            gate.block(tool_name, tool_input)
            return PermissionResultDeny(message=_DENY_MESSAGE)
        return PermissionResultAllow()

    return can_use_tool


# --- The manifest approval policy (#247): gates shipped in the bundle -----------

_MANIFEST_LOCATIONS = (Path(".claude-plugin") / "plugin.json", Path("plugin.json"))


def load_approval_policy(plugin_dir: str | None) -> dict[str, str]:
    """The bundle manifest's ``approvalPolicy`` gates as ``{tool: route}``.

    A gate's ``gate`` field names the tool the runner intercepts (the tool
    class of the ADR-0010 permission gate); its ``route`` names the approval
    route the platform binds to a channel per deployment. Declared in the
    bundle so the policy is versioned and evaluable with the agent (#247);
    validated at deploy by ``plugin_format.validate_bundle``. Best-effort,
    mirroring the hooks/systemPrompt readers: no dir, no manifest, or no
    policy yields {} and the authoritative parse gate stays load_plugins.
    """

    if not plugin_dir:
        return {}
    root = Path(plugin_dir)
    manifest_path = next(
        (root / loc for loc in _MANIFEST_LOCATIONS if (root / loc).is_file()), None
    )
    if manifest_path is None:
        return {}
    try:
        manifest = PluginManifest.model_validate(
            json.loads(manifest_path.read_text(encoding="utf-8"))
        )
        if manifest.approvalPolicy is None:
            return {}
        policy = ApprovalPolicy.model_validate(manifest.approvalPolicy)
    except (json.JSONDecodeError, ValueError, OSError):
        return {}
    return {
        gate.gate.strip(): gate.route.strip()
        for gate in policy.gates
        if gate.gate and gate.gate.strip() and gate.route and gate.route.strip()
    }
