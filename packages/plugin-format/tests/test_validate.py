from pathlib import Path

from plugin_format import validate_bundle

FIXTURES = Path(__file__).parent / "fixtures"


def _codes(path: Path) -> set[str]:
    return {issue.code for issue in validate_bundle(path).errors}


def _bundle(tmp_path: Path, manifest: str) -> Path:
    """Write a minimal bundle carrying the given manifest JSON."""
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(manifest, encoding="utf-8")
    return tmp_path


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


def test_valid_cron_and_webhook_triggers_pass(tmp_path: Path) -> None:
    bundle = _bundle(
        tmp_path,
        '{"name": "demo", "triggers": ['
        '{"type": "cron", "schedule": "0 9 * * 1-5"}, '
        '{"type": "webhook", "path": "/hooks/deploy"}]}',
    )
    assert validate_bundle(bundle).valid


def test_cron_trigger_without_schedule_is_rejected(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, '{"name": "demo", "triggers": [{"type": "cron"}]}')
    assert "triggers.cron_missing_schedule" in _codes(bundle)


def test_webhook_trigger_without_path_is_rejected(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, '{"name": "demo", "triggers": [{"type": "webhook"}]}')
    assert "triggers.webhook_missing_path" in _codes(bundle)


def test_unknown_trigger_type_is_rejected(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, '{"name": "demo", "triggers": [{"type": "kafka"}]}')
    assert "triggers.unknown_type" in _codes(bundle)


def test_malformed_triggers_shape_is_rejected(tmp_path: Path) -> None:
    # A non-list triggers value is rejected (the manifest type gate catches it).
    bundle = _bundle(tmp_path, '{"name": "demo", "triggers": "nope"}')
    assert not validate_bundle(bundle).valid


def test_trigger_entry_not_object_is_rejected(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, '{"name": "demo", "triggers": ["nope"]}')
    assert "triggers.invalid" in _codes(bundle)


def test_valid_secrets_policy_passes(tmp_path: Path) -> None:
    bundle = _bundle(
        tmp_path, '{"name": "demo", "secrets": ["GITHUB_PERSONAL_ACCESS_TOKEN", "API_KEY"]}'
    )
    assert validate_bundle(bundle).valid


def test_non_env_var_secret_name_is_rejected(tmp_path: Path) -> None:
    # A lowercase/hyphenated name cannot be forwarded as an env var -> rejected.
    bundle = _bundle(tmp_path, '{"name": "demo", "secrets": ["github-token"]}')
    assert "secrets.name_invalid" in _codes(bundle)


def test_malformed_secrets_shape_is_rejected(tmp_path: Path) -> None:
    # A non-list secrets value is rejected.
    bundle = _bundle(tmp_path, '{"name": "demo", "secrets": "nope"}')
    assert not validate_bundle(bundle).valid


def test_valid_approval_policy_passes(tmp_path: Path) -> None:
    bundle = _bundle(
        tmp_path,
        '{"name": "demo", "approvalPolicy": {"gates": ['
        '{"gate": "PreToolUse", "route": "manager-approval"}]}}',
    )
    assert validate_bundle(bundle).valid


def test_approval_gate_missing_route_is_rejected(tmp_path: Path) -> None:
    # A gate missing its 'route' field entirely -> policy fails to validate.
    bundle = _bundle(
        tmp_path, '{"name": "demo", "approvalPolicy": {"gates": [{"gate": "PreToolUse"}]}}'
    )
    assert "approval_policy.invalid" in _codes(bundle)


def test_approval_gate_empty_fields_are_rejected(tmp_path: Path) -> None:
    bundle = _bundle(
        tmp_path,
        '{"name": "demo", "approvalPolicy": {"gates": [{"gate": " ", "route": "r"}]}}',
    )
    assert "approval_policy.incomplete" in _codes(bundle)


def test_malformed_approval_policy_shape_is_rejected(tmp_path: Path) -> None:
    # A non-object approvalPolicy is rejected (the manifest type gate catches it).
    bundle = _bundle(tmp_path, '{"name": "demo", "approvalPolicy": "nope"}')
    assert not validate_bundle(bundle).valid


def test_approval_policy_gates_wrong_type_is_rejected(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, '{"name": "demo", "approvalPolicy": {"gates": "nope"}}')
    assert "approval_policy.invalid" in _codes(bundle)
