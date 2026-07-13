from pathlib import Path

from plugin_format import validate_bundle

FIXTURES = Path(__file__).parent / "fixtures"


def _codes(path: Path) -> set[str]:
    return {issue.code for issue in validate_bundle(path).errors}


def test_valid_bundle_passes() -> None:
    result = validate_bundle(FIXTURES / "valid_bundle")
    assert result.valid, result.errors
    assert result.errors == []


def test_missing_manifest_is_reported(tmp_path: Path) -> None:
    result = validate_bundle(tmp_path)
    assert not result.valid
    assert "manifest.missing" in {i.code for i in result.errors}


def test_non_directory_path_is_reported(tmp_path: Path) -> None:
    stray = tmp_path / "not-a-dir"
    stray.write_text("x", encoding="utf-8")
    result = validate_bundle(stray)
    assert not result.valid
    assert {i.code for i in result.errors} == {"bundle.missing"}


def test_non_kebab_name_is_reported() -> None:
    assert "manifest.name_invalid" in _codes(FIXTURES / "bad_manifest_name")


def test_skill_missing_description_is_reported() -> None:
    codes = _codes(FIXTURES / "bad_skill")
    assert "skill.frontmatter_invalid" in codes


def test_mcp_server_without_command_or_url_is_reported() -> None:
    assert "mcp.server_incomplete" in _codes(FIXTURES / "bad_mcp")


def test_inline_manifest_mcp_server_is_validated() -> None:
    # The manifest mcpServers field (inline object) is a supported declaration
    # and must be validated, not just a root .mcp.json file.
    assert "mcp.server_incomplete" in _codes(FIXTURES / "bad_mcp_inline")


def test_error_messages_carry_location_and_are_actionable() -> None:
    result = validate_bundle(FIXTURES / "bad_skill")
    issue = next(i for i in result.errors if i.code == "skill.frontmatter_invalid")
    assert issue.location.endswith("SKILL.md")
    assert "description" in issue.message


def _bundle(tmp_path: Path, manifest: str) -> Path:
    """Write a minimal valid bundle carrying the given manifest JSON."""
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(manifest, encoding="utf-8")
    return tmp_path


def test_inline_valid_pretooluse_hook_passes(tmp_path: Path) -> None:
    bundle = _bundle(
        tmp_path,
        '{"name": "demo", "hooks": {"PreToolUse": [{"matcher": "Bash", '
        '"hooks": [{"type": "command", "command": "./guard.sh"}]}]}}',
    )
    result = validate_bundle(bundle)
    assert result.valid, result.errors


def test_command_hook_without_command_is_rejected(tmp_path: Path) -> None:
    bundle = _bundle(
        tmp_path,
        '{"name": "demo", "hooks": {"PreToolUse": [{"matcher": "Bash", '
        '"hooks": [{"type": "command"}]}]}}',
    )
    assert "hooks.command_missing" in _codes(bundle)


def test_malformed_hooks_shape_is_rejected(tmp_path: Path) -> None:
    # A matcher entry must be an object with a hooks list, not a bare string.
    bundle = _bundle(tmp_path, '{"name": "demo", "hooks": {"PreToolUse": ["nope"]}}')
    assert "hooks.invalid" in _codes(bundle)


def test_declared_hooks_file_missing_is_rejected(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, '{"name": "demo", "hooks": "hooks/hooks.json"}')
    assert "hooks.declared_missing" in _codes(bundle)


def test_declared_hooks_file_is_validated(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, '{"name": "demo", "hooks": "hooks/hooks.json"}')
    hooks_dir = bundle / "hooks"
    hooks_dir.mkdir()
    (hooks_dir / "hooks.json").write_text(
        '{"PreToolUse": [{"hooks": [{"type": "command"}]}]}', encoding="utf-8"
    )
    assert "hooks.command_missing" in _codes(bundle)
