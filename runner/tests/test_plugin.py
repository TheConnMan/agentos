"""Plugin bundle loading and validation against the frozen plugin-format."""

from pathlib import Path

import pytest
from agentos_runner import PluginBundleError, load_plugins

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


def test_bundle_system_prompt_read_from_manifest(tmp_path: Path) -> None:
    """The manifest ``systemPrompt`` is read from the bundle (epic #30, #271)."""
    from agentos_runner import load_bundle_system_prompt

    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        '{"name": "demo", "systemPrompt": "Be terse and cite the CRM."}',
        encoding="utf-8",
    )
    assert load_bundle_system_prompt(str(tmp_path)) == "Be terse and cite the CRM."


def test_bundle_system_prompt_absent_is_none(tmp_path: Path) -> None:
    from agentos_runner import load_bundle_system_prompt

    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        '{"name": "demo"}', encoding="utf-8"
    )
    assert load_bundle_system_prompt(str(tmp_path)) is None
    # No plugin dir, and a dir with no manifest, both resolve to None.
    assert load_bundle_system_prompt(None) is None
    assert load_bundle_system_prompt("") is None


def test_bundle_system_prompt_bad_manifest_is_none(tmp_path: Path) -> None:
    """A malformed manifest is non-fatal here (load_plugins is the real gate)."""
    from agentos_runner import load_bundle_system_prompt

    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text("{ not json", encoding="utf-8")
    assert load_bundle_system_prompt(str(tmp_path)) is None
