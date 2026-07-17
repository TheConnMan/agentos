"""The shared manifest-location resolver (#636).

One helper owns the ordered manifest locations so validation, archive handling,
API bundle inspection, and the runner readers cannot drift on the path or its
precedence. These cover the three behaviors every caller relies on: the primary
path, the fallback path, and a missing manifest.
"""

from pathlib import Path

from plugin_format import MANIFEST_LOCATIONS, resolve_manifest


def test_primary_path_wins(tmp_path: Path) -> None:
    canonical = tmp_path / ".claude-plugin" / "plugin.json"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("{}", encoding="utf-8")
    # A root plugin.json also present: precedence must still pick the canonical one.
    (tmp_path / "plugin.json").write_text("{}", encoding="utf-8")

    assert resolve_manifest(tmp_path) == canonical


def test_root_plugin_json_is_the_fallback(tmp_path: Path) -> None:
    fallback = tmp_path / "plugin.json"
    fallback.write_text("{}", encoding="utf-8")

    assert resolve_manifest(tmp_path) == fallback


def test_missing_manifest_resolves_to_none(tmp_path: Path) -> None:
    assert resolve_manifest(tmp_path) is None


def test_locations_are_ordered_most_specific_first() -> None:
    assert MANIFEST_LOCATIONS == (
        Path(".claude-plugin") / "plugin.json",
        Path("plugin.json"),
    )


def test_accepts_str_root(tmp_path: Path) -> None:
    (tmp_path / "plugin.json").write_text("{}", encoding="utf-8")

    assert resolve_manifest(str(tmp_path)) == tmp_path / "plugin.json"
