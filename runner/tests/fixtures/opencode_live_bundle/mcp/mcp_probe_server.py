#!/usr/bin/env python3
"""Minimal stdlib-only MCP server over stdio for the opencode_live_bundle fixture.

Speaks JSON-RPC 2.0 (newline-delimited) implementing just enough of the Model
Context Protocol (2024-11-05) to advertise and serve one tool, ``agentos_probe``,
which returns a fixed nonce. A live turn that surfaces the nonce proves the MCP
server the compiled OpenCode config points at actually executed end to end.

No third-party dependencies: this is fixture content the bundle's .mcp.json
points at, driven as ``python3 mcp/mcp_probe_server.py``.
"""

import json
import sys

PROTOCOL_VERSION = "2024-11-05"
NONCE = "NONCE-7f3a9c"

TOOLS = [
    {
        "name": "agentos_probe",
        "description": "Return a fixed probe nonce proving the MCP server executed.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    }
]


def _send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def _respond(msg_id, result):
    _send({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _error(msg_id, code, message):
    _send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})


def _handle(request):
    method = request.get("method")
    msg_id = request.get("id")
    if method == "initialize":
        _respond(
            msg_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "agentos-probe", "version": "0.1.0"},
            },
        )
    elif method == "notifications/initialized":
        return  # notification: no response
    elif method == "tools/list":
        _respond(msg_id, {"tools": TOOLS})
    elif method == "tools/call":
        params = request.get("params") or {}
        if params.get("name") == "agentos_probe":
            _respond(
                msg_id,
                {"content": [{"type": "text", "text": NONCE}], "isError": False},
            )
        else:
            _error(msg_id, -32602, f"unknown tool: {params.get('name')}")
    elif msg_id is not None:
        _error(msg_id, -32601, f"method not found: {method}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        _handle(request)


if __name__ == "__main__":
    main()
