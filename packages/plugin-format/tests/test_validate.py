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


def test_reserved_agentos_secret_name_is_rejected(tmp_path: Path) -> None:
    # AGENTOS_* names are reserved platform boot-env keys.
    bundle = _bundle(tmp_path, '{"name": "demo", "secrets": ["AGENTOS_BUDGET"]}')
    assert "secrets.name_reserved" in _codes(bundle)


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


# --- approval gate names must be the live, fully-namespaced MCP tool name ------
#
# A bundle-declared MCP tool's live name is mcp__plugin_<bundle>_<server>__<tool>.
# The runner matches a gate by exact string equality, so an author who writes the
# obvious mcp__<server>__<tool> arms nothing: the gate silently never fires. These
# cases pin the deploy-time rejection of that shape.

_GATE_CODE = "approval_policy.gate_not_namespaced"


def _write_mcp(bundle: Path, text: str, name: str = ".mcp.json") -> Path:
    """Write an MCP declaration file into an existing bundle."""
    path = bundle / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _gate_errors(bundle: Path) -> list[str]:
    return [i.message for i in validate_bundle(bundle).errors if i.code == _GATE_CODE]


def test_bare_mcp_gate_for_declared_server_is_rejected(tmp_path: Path) -> None:
    bundle = _bundle(
        tmp_path,
        '{"name": "demo", "approvalPolicy": {"gates": ['
        '{"gate": "mcp__crm__send_contract", "route": "legal"}]}}',
    )
    _write_mcp(bundle, '{"mcpServers": {"crm": {"command": "crm-server"}}}')

    result = validate_bundle(bundle)
    assert not result.valid
    messages = [i.message for i in result.errors if i.code == _GATE_CODE]
    assert len(messages) == 1, result.errors
    message = messages[0]
    # Actionable: it must name the offending gate and the live form to use.
    assert "mcp__crm__send_contract" in message
    assert "mcp__plugin_demo_crm__" in message
    # And it must point at the escape hatch for a live name the bundle
    # does not declare, rather than dead-ending the author.
    assert "AGENTOS_APPROVAL_REQUIRED_TOOLS" in message


def test_builtin_tool_gate_passes(tmp_path: Path) -> None:
    # A gate with no mcp__ prefix names a built-in tool and is never touched,
    # even when the bundle also declares an MCP server.
    bundle = _bundle(
        tmp_path,
        '{"name": "demo", "approvalPolicy": {"gates": ['
        '{"gate": "Bash", "route": "legal"}, '
        '{"gate": "PreToolUse", "route": "manager-approval"}]}}',
    )
    _write_mcp(bundle, '{"mcpServers": {"crm": {"command": "crm-server"}}}')

    result = validate_bundle(bundle)
    assert result.valid, result.errors
    assert not [i for i in result.errors if i.code.startswith("approval_policy.")]


def test_correctly_namespaced_gate_passes_without_asserting_the_tool(tmp_path: Path) -> None:
    # send_contract is a tool nothing declares and nothing could know without
    # running the server. The prefix is correct, so the gate passes: the suffix
    # is never inspected.
    bundle = _bundle(
        tmp_path,
        '{"name": "demo", "approvalPolicy": {"gates": ['
        '{"gate": "mcp__plugin_demo_crm__send_contract", "route": "legal"}]}}',
    )
    _write_mcp(bundle, '{"mcpServers": {"crm": {"command": "crm-server"}}}')

    result = validate_bundle(bundle)
    assert result.valid, result.errors


def test_gate_naming_an_undeclared_server_is_rejected(tmp_path: Path) -> None:
    # Correct prefix shape, but 'ghost' is not a server this bundle declares.
    bundle = _bundle(
        tmp_path,
        '{"name": "demo", "approvalPolicy": {"gates": ['
        '{"gate": "mcp__plugin_demo_ghost__x", "route": "legal"}]}}',
    )
    _write_mcp(bundle, '{"mcpServers": {"crm": {"command": "crm-server"}}}')

    assert _GATE_CODE in _codes(bundle)


def test_gate_for_inline_manifest_mcp_servers_is_resolved(tmp_path: Path) -> None:
    # The manifest mcpServers field carries an inline dict rather than a path.
    good_dir = tmp_path / "good"
    good_dir.mkdir()
    good = _bundle(
        good_dir,
        '{"name": "demo", "mcpServers": {"crm": {"command": "crm-server"}}, '
        '"approvalPolicy": {"gates": ['
        '{"gate": "mcp__plugin_demo_crm__send_contract", "route": "legal"}]}}',
    )
    assert validate_bundle(good).valid, validate_bundle(good).errors

    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    bad = _bundle(
        bad_dir,
        '{"name": "demo", "mcpServers": {"crm": {"command": "crm-server"}}, '
        '"approvalPolicy": {"gates": ['
        '{"gate": "mcp__crm__send_contract", "route": "legal"}]}}',
    )
    assert _GATE_CODE in _codes(bad)


def test_gate_resolved_across_both_inline_and_root_mcp_json(tmp_path: Path) -> None:
    # A bundle with an inline dict AND a distinct root .mcp.json declares BOTH
    # sets of servers; gates for either must pass.
    bundle = _bundle(
        tmp_path,
        '{"name": "demo", "mcpServers": {"alpha": {"command": "alpha-server"}}, '
        '"approvalPolicy": {"gates": ['
        '{"gate": "mcp__plugin_demo_alpha__x", "route": "legal"}, '
        '{"gate": "mcp__plugin_demo_beta__y", "route": "legal"}]}}',
    )
    _write_mcp(bundle, '{"mcpServers": {"beta": {"command": "beta-server"}}}')

    result = validate_bundle(bundle)
    assert result.valid, result.errors


def test_gate_for_string_pointer_mcp_declaration_is_resolved(tmp_path: Path) -> None:
    # The manifest mcpServers field points at a config file rather than the
    # conventional root .mcp.json.
    bundle = _bundle(
        tmp_path,
        '{"name": "demo", "mcpServers": "config/servers.json", '
        '"approvalPolicy": {"gates": ['
        '{"gate": "mcp__plugin_demo_crm__send_contract", "route": "legal"}]}}',
    )
    _write_mcp(
        bundle, '{"mcpServers": {"crm": {"command": "crm-server"}}}', "config/servers.json"
    )

    result = validate_bundle(bundle)
    assert result.valid, result.errors


def test_mcp_gate_rejected_when_bundle_declares_no_servers(tmp_path: Path) -> None:
    # An approvalPolicy with an mcp__ gate but no MCP declaration anywhere.
    bundle = _bundle(
        tmp_path,
        '{"name": "demo", "approvalPolicy": {"gates": ['
        '{"gate": "mcp__crm__send_contract", "route": "legal"}]}}',
    )

    messages = _gate_errors(bundle)
    assert len(messages) == 1, messages
    # The message must state the bundle declares none rather than print an
    # empty list at the author.
    assert "no MCP servers" in messages[0]


def test_invalid_json_mcp_declaration_does_not_add_a_gate_error(tmp_path: Path) -> None:
    # An unreadable declaration is not an empty one: the bundle already fails on
    # the MCP error, and stacking a misleading gate error on top would send the
    # author chasing the wrong fix.
    bundle = _bundle(
        tmp_path,
        '{"name": "demo", "approvalPolicy": {"gates": ['
        '{"gate": "mcp__crm__send_contract", "route": "legal"}]}}',
    )
    _write_mcp(bundle, "{not json")

    codes = _codes(bundle)
    assert "mcp.invalid_json" in codes
    assert _GATE_CODE not in codes


def test_invalid_mcp_config_does_not_add_a_gate_error(tmp_path: Path) -> None:
    # Valid JSON that fails McpConfig validation. This is the layer where
    # conflating "could not read" with "read, and it was empty" is the tempting
    # shortcut: an empty set here would report every mcp__ gate as naming an
    # undeclared server, on top of the real mcp.invalid error.
    bundle = _bundle(
        tmp_path,
        '{"name": "demo", "approvalPolicy": {"gates": ['
        '{"gate": "mcp__crm__send_contract", "route": "legal"}]}}',
    )
    _write_mcp(bundle, '{"mcpServers": []}')

    codes = _codes(bundle)
    assert "mcp.invalid" in codes
    assert _GATE_CODE not in codes


def test_declared_missing_mcp_path_does_not_add_a_gate_error(tmp_path: Path) -> None:
    # A manifest mcpServers path that was not found is also unreadable.
    bundle = _bundle(
        tmp_path,
        '{"name": "demo", "mcpServers": "config/servers.json", '
        '"approvalPolicy": {"gates": ['
        '{"gate": "mcp__crm__send_contract", "route": "legal"}]}}',
    )

    codes = _codes(bundle)
    assert "mcp.declared_missing" in codes
    assert _GATE_CODE not in codes


def test_bundle_declaring_zero_mcp_servers_still_rejects_an_mcp_gate(tmp_path: Path) -> None:
    # The other side of the unreadable cases: a READABLE declaration that
    # declares no servers. Same empty prefix set, opposite verdict. This pair is
    # what makes empty and unreadable provably distinct facts.
    bundle = _bundle(
        tmp_path,
        '{"name": "demo", "approvalPolicy": {"gates": ['
        '{"gate": "mcp__crm__send_contract", "route": "legal"}]}}',
    )
    _write_mcp(bundle, '{"mcpServers": {}}')

    codes = _codes(bundle)
    assert "mcp.invalid" not in codes
    assert "mcp.invalid_json" not in codes
    assert _GATE_CODE in codes


def test_hyphenated_bundle_and_underscored_server_names_resolve(tmp_path: Path) -> None:
    # Live names are not mangled: a bundle name keeps its hyphens and a server
    # key keeps its underscores. This is why the rule constructs the expected
    # prefix from what the bundle declares instead of parsing the gate string.
    manifest = (
        '{{"name": "github-issues", '
        '"mcpServers": {{"local_tools": {{"command": "tools-server"}}}}, '
        '"approvalPolicy": {{"gates": [{{"gate": "{gate}", "route": "legal"}}]}}}}'
    )
    good_dir = tmp_path / "good"
    good_dir.mkdir()
    good = _bundle(
        good_dir, manifest.format(gate="mcp__plugin_github-issues_local_tools__x")
    )
    assert validate_bundle(good).valid, validate_bundle(good).errors

    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    bad = _bundle(bad_dir, manifest.format(gate="mcp__local_tools__x"))
    bad_messages = _gate_errors(bad)
    assert len(bad_messages) == 1, bad_messages
    assert "mcp__plugin_github-issues_local_tools__" in bad_messages[0]


def test_malformed_mcp_gate_is_rejected(tmp_path: Path) -> None:
    # A gate that is bare 'mcp__' or otherwise matches no expected prefix falls
    # to the general rule; no special case exists for it.
    bundle = _bundle(
        tmp_path,
        '{"name": "demo", "approvalPolicy": {"gates": ['
        '{"gate": "mcp__", "route": "legal"}]}}',
    )
    _write_mcp(bundle, '{"mcpServers": {"crm": {"command": "crm-server"}}}')

    assert _GATE_CODE in _codes(bundle)


def test_gate_with_leading_whitespace_is_rejected(tmp_path: Path) -> None:
    # Leading whitespace hides the mcp__ prefix from a naive startswith check,
    # so the gate looks like a built-in tool and passes green. But the runner
    # strips the value before matching, leaving the bare mcp__crm__send_contract
    # that never equals the live mcp__plugin_demo_crm__send_contract -- the gate
    # arms nothing. The validator must inspect the stripped value, like runtime.
    bundle = _bundle(
        tmp_path,
        '{"name": "demo", "approvalPolicy": {"gates": ['
        '{"gate": " mcp__crm__send_contract", "route": "legal"}]}}',
    )
    _write_mcp(bundle, '{"mcpServers": {"crm": {"command": "crm-server"}}}')

    assert _GATE_CODE in _codes(bundle)


def test_mcp_gate_with_empty_tool_suffix_is_rejected(tmp_path: Path) -> None:
    # The prefix is correct but there is no tool name after it, so the gate can
    # never equal a real tool like mcp__plugin_demo_crm__send_contract. The
    # startswith check alone passes it green; the validator must require at least
    # one character after the matched prefix.
    bundle = _bundle(
        tmp_path,
        '{"name": "demo", "approvalPolicy": {"gates": ['
        '{"gate": "mcp__plugin_demo_crm__", "route": "legal"}]}}',
    )
    _write_mcp(bundle, '{"mcpServers": {"crm": {"command": "crm-server"}}}')

    assert _GATE_CODE in _codes(bundle)


def test_each_offending_gate_is_reported_at_its_own_location(tmp_path: Path) -> None:
    # Gates are checked independently at gates[i]; no dedupe.
    bundle = _bundle(
        tmp_path,
        '{"name": "demo", "approvalPolicy": {"gates": ['
        '{"gate": "mcp__crm__send_contract", "route": "legal"}, '
        '{"gate": "mcp__crm__send_contract", "route": "finance"}]}}',
    )
    _write_mcp(bundle, '{"mcpServers": {"crm": {"command": "crm-server"}}}')

    locations = [
        i.location for i in validate_bundle(bundle).errors if i.code == _GATE_CODE
    ]
    # Both entries are reported, each anchored to its own gates[i].
    assert len(locations) == 2, locations
    assert any("gates[0]" in loc for loc in locations)
    assert any("gates[1]" in loc for loc in locations)
