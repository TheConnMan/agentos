"""The text-stats-engine template validates and its in-bundle engine runs.

Guards the blessed "engine as an in-bundle stdio MCP server" template (#274):
the bundle stays byte-compatible with ``plugin_format.validate_bundle`` and the
engine speaks the MCP stdio transport end-to-end without a model or the network.
"""

import json
import subprocess
import sys
from pathlib import Path

from plugin_format import validate_bundle

BUNDLE = Path(__file__).resolve().parents[1] / "text-stats-engine"


def test_template_bundle_validates() -> None:
    result = validate_bundle(BUNDLE)
    assert result.valid, result.errors
    assert result.errors == []


def _drive(*requests: dict) -> list[dict]:
    payload = "".join(json.dumps(r) + "\n" for r in requests)
    proc = subprocess.run(
        [sys.executable, "scripts/engine_server.py"],
        cwd=BUNDLE,
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def test_engine_handshake_and_tools_list() -> None:
    responses = _drive(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert responses[0]["result"]["serverInfo"]["name"] == "text-stats-engine"
    tool_names = {t["name"] for t in responses[1]["result"]["tools"]}
    assert tool_names == {"word_count", "char_count", "reading_time_minutes"}


def test_engine_tool_call_returns_result() -> None:
    (response,) = _drive(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "word_count", "arguments": {"text": "one two three"}},
        }
    )
    assert response["result"]["content"][0]["text"] == "3"
    assert response["result"]["isError"] is False


def test_engine_notification_gets_no_reply() -> None:
    # Notifications carry no id and must produce no response line.
    responses = _drive({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert responses == []


def test_engine_unknown_tool_is_an_error() -> None:
    (response,) = _drive(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "nope", "arguments": {}},
        }
    )
    assert response["error"]["code"] == -32602
