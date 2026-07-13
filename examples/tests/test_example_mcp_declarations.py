"""Every example bundle declares its MCP servers in a form the real runner loads.

Two landmines the ``agentos guide`` documents get enforced here across *all*
example bundles (discovered dynamically, so a future third example is covered
without editing this file):

1. A plugin manifest's ``mcpServers`` must be an inline **object**, never a
   string path. The string-pointer form (``"mcpServers": ".mcp.json"``) is
   silently ignored by Claude Code, disabling MCP loading entirely.
2. An in-bundle stdio server's script reference must be cwd-independent
   (``${CLAUDE_PLUGIN_ROOT}``-qualified or absolute). A bare relative path like
   ``scripts/engine_server.py`` only spawns when cwd happens to be the bundle
   root. This is checked on both ``command`` and every ``args`` entry, for any
   interpreter (python, node, shell, ...), so a future non-python example is
   covered too.
"""

import json
import os
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parents[1]

# A string that ends in one of these is a reference to a script shipped inside
# the bundle -- it has to resolve against the bundle root, so it must be
# cwd-independent. A bare interpreter name (``python3``, ``node``) has no such
# extension and is resolved from PATH, so it is correctly left alone.
_SCRIPT_SUFFIXES = (".py", ".js", ".mjs", ".cjs", ".ts", ".sh", ".rb")


def _discover_bundles() -> list[Path]:
    """Immediate ``examples/`` subdirs that carry a plugin manifest."""
    return sorted(
        child
        for child in EXAMPLES.iterdir()
        if child.is_dir()
        and child.name != "tests"
        and (child / ".claude-plugin" / "plugin.json").is_file()
    )


def _is_bundle_script_ref(value: object) -> bool:
    return isinstance(value, str) and value.endswith(_SCRIPT_SUFFIXES)


def test_no_example_manifest_uses_string_pointer_mcpservers() -> None:
    violations: list[str] = []
    for bundle in _discover_bundles():
        manifest = json.loads((bundle / ".claude-plugin" / "plugin.json").read_text())
        if "mcpServers" not in manifest:
            continue
        value = manifest["mcpServers"]
        if not isinstance(value, dict):
            violations.append(
                f"{bundle.name}: mcpServers must be an inline object, got "
                f"{type(value).__name__} {value!r} (string-pointer form silently "
                f"disables MCP loading)"
            )
    assert not violations, "String-pointer mcpServers declarations found:\n" + "\n".join(
        violations
    )


def test_example_mcp_server_script_args_are_cwd_independent() -> None:
    violations: list[str] = []
    for bundle in _discover_bundles():
        mcp_path = bundle / ".mcp.json"
        if not mcp_path.is_file():
            continue
        servers = json.loads(mcp_path.read_text()).get("mcpServers", {})
        for name, spec in servers.items():
            candidates = [spec.get("command", ""), *spec.get("args", [])]
            for value in candidates:
                if not _is_bundle_script_ref(value):
                    continue
                if value.startswith("${CLAUDE_PLUGIN_ROOT}") or os.path.isabs(value):
                    continue
                violations.append(
                    f"{bundle.name}/{name}: script reference {value!r} is a bare "
                    f"relative path; use a ${{CLAUDE_PLUGIN_ROOT}}-qualified or "
                    f"absolute path so the server spawns regardless of cwd"
                )
    assert not violations, "cwd-dependent MCP server script args found:\n" + "\n".join(
        violations
    )
