#!/usr/bin/env python3
"""In-bundle stdio MCP server: the "engine" this template ships.

This is the blessed "engine as an in-bundle stdio MCP server" shape (#274). The
engine's tools live *inside the bundle* and are spawned as a stdio subprocess by
the harness via the ``.mcp.json`` declaration — no network service, no hosted
sidecar, nothing to provision. A hosted harness that can only reach remote MCP
URLs cannot do this; running the engine in-process next to the agent is the
differentiator this template paves.

Transport: the MCP stdio transport — newline-delimited JSON-RPC 2.0 on stdin /
stdout (one message per line). Deliberately dependency-free (Python stdlib only)
so the template runs end-to-end from a clean clone with no install step.

Implements just enough of MCP to be a real, drivable server:
    initialize        -> protocol handshake
    tools/list        -> advertise this engine's tools
    tools/call        -> run a tool and return its result

The tools are a tiny deterministic "text statistics" engine so the template is
verifiable without a model or the network.
"""

from __future__ import annotations

import json
import sys
from typing import Any

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "text-stats-engine"
SERVER_VERSION = "0.1.0"

# Tool catalog advertised via tools/list. Each entry carries the JSON Schema the
# MCP client uses to validate arguments before calling.
TOOLS: list[dict[str, Any]] = [
    {
        "name": "word_count",
        "description": "Count the words in a piece of text.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "char_count",
        "description": "Count the characters in a piece of text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "include_whitespace": {"type": "boolean", "default": True},
            },
            "required": ["text"],
        },
    },
    {
        "name": "reading_time_minutes",
        "description": "Estimate reading time in minutes at 200 words per minute.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
]


def _run_tool(name: str, arguments: dict[str, Any]) -> str:
    text = str(arguments.get("text", ""))
    if name == "word_count":
        return str(len(text.split()))
    if name == "char_count":
        if arguments.get("include_whitespace", True):
            return str(len(text))
        return str(len("".join(text.split())))
    if name == "reading_time_minutes":
        words = len(text.split())
        # Round up to a whole minute; empty text reads in 0 minutes.
        return str(-(-words // 200)) if words else "0"
    raise KeyError(name)


def _handle(request: dict[str, Any]) -> dict[str, Any] | None:
    """Return a JSON-RPC response, or None for a notification (no reply)."""

    method = request.get("method")
    req_id = request.get("id")

    # Notifications (e.g. notifications/initialized) carry no id and get no reply.
    if req_id is None:
        return None

    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        try:
            output = _run_tool(name, arguments)
        except KeyError:
            return _error(req_id, -32602, f"unknown tool: {name!r}")
        result = {"content": [{"type": "text", "text": output}], "isError": False}
    else:
        return _error(req_id, -32601, f"method not found: {method!r}")

    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = _handle(request)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
