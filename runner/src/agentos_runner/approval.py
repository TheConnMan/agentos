"""The in-process approval-request tool: how a skill raises a policy gate.

ADR-0010 (approval gates, epic #22) makes gates policy-triggered, never
phase-hardcoded: the agent's own logic decides something needs a human
decision and raises an approval request. This module is that primitive's
runner half. It registers an in-process SDK MCP tool
(``mcp__agentos__request_approval``) every session carries, so a skill
instructs the model to call it with a one-line summary of what needs
approval. The call itself executes no real-world action; it only marks the
turn, and the session emits its terminal ``final`` with
``status=awaiting-approval`` and the captured summary (see ``session.py``).

The durable ``Approval`` record, the authorizer, and the resume trigger all
live server-side with the worker/API (they must not be spoofable from inside
the sandbox -- see docs/interfaces/approval/INTERFACE.md); the runner's only
job is to end the turn in the awaiting-approval state.

Detection is wire-level, not execution-level: ``translate.py`` captures the
``ToolUseBlock`` naming this tool, so the fake-model path (a scripted
``ToolUseBlock``, no tool execution, no network) exercises the identical seam
as a real model call.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

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
