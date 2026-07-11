"""Plugin bundle loading and validation against the frozen plugin-format."""

from pathlib import Path

import pytest
from agentos_runner import (
    BundleInstaller,
    ClaudeBundleInstaller,
    PluginBundleError,
    load_plugins,
)

_FIXTURES = Path(__file__).resolve().parents[2] / "packages/plugin-format/tests/fixtures"


def test_no_plugin_dir_is_empty() -> None:
    assert load_plugins(None) == []
    assert load_plugins("") == []


def test_valid_bundle_becomes_local_plugin_config() -> None:
    bundle = _FIXTURES / "valid_bundle"
    plugins = load_plugins(str(bundle))
    assert plugins == [{"type": "local", "path": str(bundle)}]


def test_invalid_bundle_raises() -> None:
    bundle = _FIXTURES / "bad_manifest_name"
    with pytest.raises(PluginBundleError):
        load_plugins(str(bundle))


def test_claude_installer_passthrough_matches_load_plugins() -> None:
    bundle = _FIXTURES / "valid_bundle"
    installer = ClaudeBundleInstaller()
    assert installer.install(str(bundle)) == load_plugins(str(bundle))
    assert installer.install(str(bundle)) == [{"type": "local", "path": str(bundle)}]


def test_claude_installer_no_dir_is_empty() -> None:
    assert ClaudeBundleInstaller().install(None) == []


def test_claude_installer_invalid_bundle_raises() -> None:
    bundle = _FIXTURES / "bad_manifest_name"
    with pytest.raises(PluginBundleError):
        ClaudeBundleInstaller().install(str(bundle))


def test_claude_installer_satisfies_bundle_installer_port() -> None:
    assert isinstance(ClaudeBundleInstaller(), BundleInstaller)
