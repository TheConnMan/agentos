"""Load and validate the mounted plugin bundle for the SDK.

``AGENTOS_PLUGIN_DIR`` points at a Claude Code plugin bundle (skills/, .mcp.json,
scripts/, plugin.json). The runner validates it with the frozen
``plugin_format.validate_bundle`` before handing it to the SDK, and translates a
valid bundle into the ``ClaudeAgentOptions.plugins`` shape (a local plugin
config). An invalid bundle is a hard configuration error surfaced at startup, not
a silent skip: a runner that booted with a broken bundle would answer with the
wrong (empty) capability set.
"""

import json
from pathlib import Path

from claude_agent_sdk import SdkPluginConfig
from plugin_format import PluginManifest, validate_bundle

# The manifest lives at .claude-plugin/plugin.json; a bare plugin.json at the
# bundle root is accepted as a fallback, mirroring plugin_format's own lookup.
_MANIFEST_LOCATIONS = (Path(".claude-plugin") / "plugin.json", Path("plugin.json"))


class PluginBundleError(RuntimeError):
    """Raised when the mounted plugin bundle fails validation."""


def load_bundle_system_prompt(plugin_dir: str | None) -> str | None:
    """Return the ``systemPrompt`` declared in the bundle manifest, if any.

    The system prompt travels in the bundle (manifest field, epic #30) so it is
    versioned with the agent rather than supplied only out-of-band via
    ``AGENTOS_SYSTEM_PROMPT``. Returns ``None`` when there is no plugin dir, no
    manifest, or no ``systemPrompt`` field. Best-effort and non-fatal: a bundle
    that fails to parse here is caught by ``load_plugins`` at startup, which is
    the authoritative validation gate, so this reader stays quiet.
    """

    if not plugin_dir:
        return None
    root = Path(plugin_dir)
    manifest_path = next(
        (root / loc for loc in _MANIFEST_LOCATIONS if (root / loc).is_file()), None
    )
    if manifest_path is None:
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = PluginManifest.model_validate(data)
    except (json.JSONDecodeError, ValueError, OSError):
        return None
    return manifest.systemPrompt


def load_plugins(plugin_dir: str | None) -> list[SdkPluginConfig]:
    """Validate the bundle at ``plugin_dir`` and return the SDK plugin config.

    Returns an empty list when no plugin dir is configured. Raises
    ``PluginBundleError`` with the aggregated validation issues when the bundle
    exists but is malformed.
    """

    if not plugin_dir:
        return []

    root = Path(plugin_dir)
    result = validate_bundle(root)
    if not result.valid:
        detail = "; ".join(f"[{i.code}] {i.location}: {i.message}" for i in result.errors)
        raise PluginBundleError(f"invalid plugin bundle at {root}: {detail}")

    return [SdkPluginConfig(type="local", path=str(root))]
