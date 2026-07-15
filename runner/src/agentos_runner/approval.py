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
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import (
    CanUseTool,
    McpSdkServerConfig,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

# The server key under ClaudeAgentOptions.mcp_servers; the SDK prefixes tool
# names as mcp__<server>__<tool>.
APPROVAL_SERVER_NAME = "agentos"
_TOOL_NAME = "request_approval"
# The fully qualified tool identifier as it appears on ToolUseBlock.name.
APPROVAL_TOOL_NAME = f"mcp__{APPROVAL_SERVER_NAME}__{_TOOL_NAME}"

_TOOL_DESCRIPTION = (
    "Request human approval before proceeding. Call this when your"
    " instructions say a step needs sign-off (a discount, an invoice, a"
    " remediation). Pass a one-line summary of exactly what needs approval."
    " After calling it, end your turn and tell the user the request is"
    " pending; the platform pauses the session and resumes it with the"
    " decision once an authorized human resolves it."
)


@tool(_TOOL_NAME, _TOOL_DESCRIPTION, {"summary": str})
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
    return f"Tool call awaiting approval: {tool_name} {rendered}"


@dataclass
class ApprovalGate:
    """Shared state between the ``can_use_tool`` callback and the session loop.

    ``required`` is the per-agent set of approval-required tool names
    (AGENTOS_APPROVAL_REQUIRED_TOOLS). ``pending_summary`` is set by the
    callback when it blocks a call and consumed by the session at turn end to
    flip the final to awaiting-approval; ``reset()`` clears it at each turn
    start so one turn's block never leaks into the next.

    The first blocked call of a turn wins: a model that retries the denied
    tool (against the deny message's instruction) does not overwrite the
    summary the human will resolve against.
    """

    required: frozenset[str] = field(default_factory=frozenset)
    pending_summary: str | None = None

    def reset(self) -> None:
        self.pending_summary = None

    def block(self, tool_name: str, tool_input: dict[str, Any]) -> None:
        if self.pending_summary is None:
            self.pending_summary = summarize_tool_call(tool_name, tool_input)


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
            gate.block(tool_name, tool_input)
            return PermissionResultDeny(message=_DENY_MESSAGE)
        return PermissionResultAllow()

    return can_use_tool
