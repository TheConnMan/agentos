"""Load and validate the mounted plugin bundle for the SDK.

``AGENTOS_PLUGIN_DIR`` points at a Claude Code plugin bundle (skills/, .mcp.json,
scripts/, plugin.json). The runner validates it with the frozen
``plugin_format.validate_bundle`` before handing it to the SDK, and translates a
valid bundle into the ``ClaudeAgentOptions.plugins`` shape (a local plugin
config). An invalid bundle is a hard configuration error surfaced at startup, not
a silent skip: a runner that booted with a broken bundle would answer with the
wrong (empty) capability set.

Bundle ingestion sits behind the :class:`BundleInstaller` port so a non-Claude
harness can interpret the same validated bundle into its own session config
(Decision 3 of the harness-neutral runner seams ADR, PR #306). The Claude
implementation
(:class:`ClaudeBundleInstaller`) is the passthrough that hands the bundle straight
to the SDK; the OpenCode harness supplies its own installer (see
``opencode/installer.py``). The port's output type is generic because each
harness contributes a different native config shape.
"""

from pathlib import Path
from typing import Protocol, TypeVar, runtime_checkable

from claude_agent_sdk import SdkPluginConfig
from plugin_format import validate_bundle

T_co = TypeVar("T_co", covariant=True)


class PluginBundleError(RuntimeError):
    """Raised when the mounted plugin bundle fails validation."""


@runtime_checkable
class BundleInstaller(Protocol[T_co]):
    """Ingest a validated plugin bundle into a harness-native session config.

    The single seam where a harness turns the mounted bundle at ``plugin_dir``
    into its own session-config contribution: validate the bundle and return this
    harness's native config, raise :class:`PluginBundleError` on an invalid
    bundle, and treat a ``None`` or absent dir as "no bundle configured" by
    returning the harness's empty configuration. ``T_co`` is the harness-native
    config type (a Claude plugin-config list, ``None`` for the OpenCode stub, and
    so on), so the contract is one shape while the payload stays per harness.
    """

    def install(self, plugin_dir: str | None) -> T_co: ...


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


class ClaudeBundleInstaller:
    """The Claude harness :class:`BundleInstaller`: the passthrough.

    Hands the validated bundle straight to the SDK unchanged, which is exactly
    ``load_plugins`` -- the installer delegates to it so the passthrough stays the
    single source of the Claude ingestion behavior. Named by Decision 3 of the
    harness-neutral runner seams ADR (PR #306) as the current passthrough seam
    that a non-Claude harness replaces.
    """

    def install(self, plugin_dir: str | None) -> list[SdkPluginConfig]:
        return load_plugins(plugin_dir)
