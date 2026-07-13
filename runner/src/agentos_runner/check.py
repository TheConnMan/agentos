"""Offline, credential-free MCP load check (issue #337).

Run as ``python -m agentos_runner.check`` with ``AGENTOS_PLUGIN_DIR`` pointing at a
Claude Code plugin bundle. The check:

1. validates the bundle via the frozen ``load_plugins`` (``PluginBundleError`` ->
   ``invalid_bundle``),
2. parses the bundle's **declared** MCP servers (``extract_declared``),
3. builds a real ``ClaudeSDKClient`` via ``build_options`` and ``connect()`` -- no
   query, no model turn -- then polls ``get_mcp_status()`` until the bundle's own
   servers settle, and
4. compares declared intent against the plugin-owned registered servers
   (``evaluate``), emitting exactly one JSON object to **stdout** (all logging to
   **stderr**) and exiting 0 (green) / 1 (red) / 2 (invalid_bundle).

There is **no credential path**: the spike verified ``connect()`` succeeds with
zero credential and an empty HOME, and this module never issues a ``query()``.
The runner<->CLI JSON seam is frozen in the plan (Section 3).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeSDKClient

from .adapter import build_options
from .plugin import PluginBundleError, load_plugins

logger = logging.getLogger(__name__)

CHECK_NAME = "mcp-load"
CHECK_VERSION = 1
_DEFAULT_TIMEOUT_S = 30
_POLL_INTERVAL_S = 1.0

_STRING_POINTER_HINT = (
    "plugin.json 'mcpServers' is a string pointer; the real loader silently "
    "ignores this form — inline the object"
)

_EXIT_CODES = {"green": 0, "red": 1, "invalid_bundle": 2}


# --------------------------------------------------------------------------- #
# Declared-server extraction (bundle intent)
# --------------------------------------------------------------------------- #
def extract_declared(plugin_dir: str) -> list[dict[str, Any]]:
    """Parse the bundle's declared MCP servers into ``{name, source, form}`` rows.

    Covers all three declaration forms (plan Section 3): a ``plugin.json``
    ``mcpServers`` object (``inline``), a ``mcpServers`` string pointer resolved
    relative to the bundle root (``string_pointer``; a missing pointed file still
    surfaces the intent), and a bare-root ``.mcp.json`` (``bare_file``, deduped by
    name against the above).
    """

    root = Path(plugin_dir)
    declared: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(name: str, source: str, form: str) -> None:
        if name not in seen:
            declared.append({"name": name, "source": source, "form": form})
            seen.add(name)

    manifest = _read_json(root / ".claude-plugin" / "plugin.json")
    mcp = manifest.get("mcpServers") if isinstance(manifest, dict) else None

    if isinstance(mcp, dict):
        for name in mcp:
            _add(str(name), "plugin.json", "inline")
    elif isinstance(mcp, str):
        pointed = _read_json(root / mcp)
        names = _server_names(pointed)
        if names:
            for name in names:
                _add(name, "plugin.json", "string_pointer")
        else:
            # Pointed file missing/unparseable: the intent (a string-pointer
            # declaration) still surfaces so the caller can drive a red verdict.
            manifest_name = manifest.get("name") if isinstance(manifest, dict) else None
            fallback = str(manifest_name) if manifest_name else mcp
            _add(fallback, "plugin.json", "string_pointer")

    bare = _read_json(root / ".mcp.json")
    for name in _server_names(bare):
        _add(name, ".mcp.json", "bare_file")

    return declared


def _server_names(payload: Any) -> list[str]:
    if isinstance(payload, dict):
        servers = payload.get("mcpServers")
        if isinstance(servers, dict):
            return [str(name) for name in servers]
    return []


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------- #
# Verdict (pure: no I/O, no SDK)
# --------------------------------------------------------------------------- #
def evaluate(
    declared: list[dict[str, Any]], registered: list[dict[str, Any]]
) -> dict[str, Any]:
    """Compute the verdict from declared intent vs registered MCP servers.

    The registered list is ``McpServerStatus``-shaped. Only the bundle's **own**
    servers (``scope == "dynamic"`` or a ``plugin:``-prefixed name when scope is
    absent) affect the verdict; ambient project/user servers never do. The rule is
    declared-anchored: one match row per declared server, green iff every declared
    server matched a connected own-server with at least one tool (plan Section 3).
    """

    own = [s for s in registered if _plugin_owned(s)]
    matches: list[dict[str, Any]] = []
    reasons: list[str] = []
    hints: list[str] = []

    if any(d.get("form") == "string_pointer" for d in declared):
        hints.append(_STRING_POINTER_HINT)

    connected_with_tools = 0
    for row in declared:
        name = str(row["name"])
        match = _find_match(name, own)
        tool_count = len(match.get("tools") or []) if match is not None else 0
        connected = match is not None and match.get("status") == "connected" and tool_count >= 1
        if connected:
            connected_with_tools += 1
        else:
            reasons.append(_reason_for(name, match))
        matches.append(
            {
                "declared": name,
                "registered": match["name"] if match is not None else None,
                "connected": connected,
                "tool_count": tool_count,
            }
        )

    if declared and connected_with_tools == 0:
        reasons.append(
            f"declared {len(declared)} MCP server(s); none registered with tools"
        )

    verdict = "red" if reasons else "green"
    return {"matches": matches, "verdict": verdict, "reasons": reasons, "hints": hints}


def _plugin_owned(status: dict[str, Any]) -> bool:
    scope = status.get("scope")
    if scope == "dynamic":
        return True
    return scope is None and str(status.get("name", "")).startswith("plugin:")


def _find_match(name: str, own: list[dict[str, Any]]) -> dict[str, Any] | None:
    for server in own:
        registered_name = str(server.get("name", ""))
        if registered_name == name or registered_name.endswith(":" + name):
            return server
    return None


def _reason_for(name: str, match: dict[str, Any] | None) -> str:
    if match is None:
        return f"declared {name} never registered"
    status = match.get("status")
    if status == "failed":
        error = match.get("error")
        return str(error) if error else f"declared {name} failed to connect"
    if status == "pending":
        return f"declared {name} did not finish initializing"
    if status == "needs-auth":
        return (
            f"declared {name} needs authentication; the offline check is "
            "credential-free and cannot validate a credential-gated server"
        )
    if status == "connected":
        return f"declared {name} connected with zero tools"
    return f"declared {name} registered with status {status!r} and no tools"


# --------------------------------------------------------------------------- #
# Real-loader run
# --------------------------------------------------------------------------- #
async def run_check(plugin_dir: str) -> dict[str, Any]:
    """Validate, connect, poll, and assemble the full Section-3 result dict."""

    try:
        plugins = load_plugins(plugin_dir)
    except PluginBundleError as exc:
        return _invalid_bundle_result(plugin_dir, [str(exc)])

    declared = extract_declared(plugin_dir)
    timeout_s = _timeout_s()

    try:
        registered = await asyncio.wait_for(_connect_and_poll(plugins), timeout_s)
    except TimeoutError:
        return _red_result(
            plugin_dir, declared, f"MCP init did not complete within {timeout_s}s"
        )
    except Exception as exc:
        # A non-timeout failure while setting up the MCP client (Claude CLI
        # subprocess fails to start, an incompatible --image, an SDK error)
        # would otherwise escape main() before any JSON is printed, and the CLI
        # would report an opaque contract violation. Surface it as a valid RED
        # result naming the failure instead of swallowing it. A genuinely
        # malformed bundle is still invalid_bundle (caught above), not red.
        return _red_result(
            plugin_dir,
            declared,
            f"MCP client failed to start: {type(exc).__name__}: {exc}",
        )

    return _assemble(plugin_dir, declared, registered, evaluate(declared, registered))


def _red_result(
    plugin_dir: str, declared: list[dict[str, Any]], reason: str
) -> dict[str, Any]:
    """Assemble a RED result (no registered servers) with a single override reason.

    Shared by the timeout and MCP-client-startup failure paths in ``run_check``:
    both assemble the base result from empty registered servers, then force the
    verdict red with their own single reason string.
    """

    result = _assemble(plugin_dir, declared, [], evaluate(declared, []))
    result["verdict"] = "red"
    result["reasons"] = [reason]
    return result


async def _connect_and_poll(plugins: list[Any]) -> list[dict[str, Any]]:
    """Connect a real client and poll get_mcp_status until own servers settle.

    Runs no query. Returns the verbatim ``mcpServers`` list once no plugin-owned
    server is still ``pending`` (ambient servers included for transparency). The
    caller wraps this in ``asyncio.wait_for``; ``disconnect()`` runs on every path.
    """

    options = build_options(
        plugins=plugins,
        model=None,
        system_prompt=None,
        max_turns=1,
        max_budget_usd=None,
        resume=None,
    )
    client = ClaudeSDKClient(options)
    await client.connect()
    try:
        while True:
            status = await client.get_mcp_status()
            servers = [dict(s) for s in status.get("mcpServers", [])]
            pending = [
                s
                for s in servers
                if _plugin_owned(s) and s.get("status") == "pending"
            ]
            if not pending:
                return servers
            await asyncio.sleep(_POLL_INTERVAL_S)
    finally:
        await client.disconnect()


def _assemble(
    plugin_dir: str,
    declared: list[dict[str, Any]],
    registered: list[dict[str, Any]],
    verdict: dict[str, Any],
) -> dict[str, Any]:
    return {
        "check": CHECK_NAME,
        "version": CHECK_VERSION,
        "plugin_dir": plugin_dir,
        "declared": declared,
        "registered": registered,
        "matches": verdict["matches"],
        "verdict": verdict["verdict"],
        "reasons": verdict["reasons"],
        "hints": verdict["hints"],
    }


def _invalid_bundle_result(plugin_dir: str, reasons: list[str]) -> dict[str, Any]:
    return {
        "check": CHECK_NAME,
        "version": CHECK_VERSION,
        "plugin_dir": plugin_dir,
        "declared": [],
        "registered": [],
        "matches": [],
        "verdict": "invalid_bundle",
        "reasons": reasons,
        "hints": [],
    }


def _timeout_s() -> int:
    raw = os.environ.get("AGENTOS_CHECK_TIMEOUT_S")
    if not raw:
        return _DEFAULT_TIMEOUT_S
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_TIMEOUT_S
    return value if value > 0 else _DEFAULT_TIMEOUT_S


# --------------------------------------------------------------------------- #
# Module entry
# --------------------------------------------------------------------------- #
def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    plugin_dir = os.environ.get("AGENTOS_PLUGIN_DIR")
    if not plugin_dir or not Path(plugin_dir).is_dir():
        result = _invalid_bundle_result(
            plugin_dir or "", [f"plugin dir not found: {plugin_dir!r}"]
        )
        print(json.dumps(result))
        return _EXIT_CODES["invalid_bundle"]

    result = asyncio.run(run_check(plugin_dir))
    print(json.dumps(result))
    return _EXIT_CODES.get(str(result["verdict"]), 1)


if __name__ == "__main__":
    sys.exit(main())
