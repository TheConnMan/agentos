"""Load and validate the mounted plugin bundle for the SDK.

``AGENTOS_PLUGIN_DIR`` points at a Claude Code plugin bundle (skills/, .mcp.json,
scripts/, plugin.json). The runner validates it with the frozen
``plugin_format.validate_bundle`` before handing it to the SDK, and translates a
valid bundle into the ``ClaudeAgentOptions.plugins`` shape (a local plugin
config). An invalid bundle is a hard configuration error surfaced at startup, not
a silent skip: a runner that booted with a broken bundle would answer with the
wrong (empty) capability set.
"""

from pathlib import Path

from claude_agent_sdk import SdkPluginConfig
from plugin_format import validate_bundle


class PluginBundleError(RuntimeError):
    """Raised when the mounted plugin bundle fails validation."""


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
