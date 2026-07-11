"""The OpenCode bundle compiler: validated plugin bundle -> OpenCode session workdir.

Pins the #310 compiler contract (plan sections 4-8): ``install()`` validates a
bundle then materializes a temp workdir carrying ``opencode.json`` (MCP), copied
skills under ``.claude/skills/``, and commands/agents under ``.opencode/``. Every
test asserts compiled *content* (bytes, parsed JSON, warning text), never mere
file existence. No mocks -- the compiler is pure filesystem + JSON.

The import of ``CompiledOpenCodeBundle`` fails until the compiler is written; that
collection error is the intended pre-implementation state.
"""

import json
import logging
from pathlib import Path
from typing import cast

import pytest
import yaml
from agentos_runner.opencode import CompiledOpenCodeBundle, OpenCodeBundleInstaller
from agentos_runner.opencode.conformance import (
    _BUNDLE_MCP_NONCE,
    _BUNDLE_SKILL_TOKEN,
    _build_runner,
    _bundle_demo_failures,
)
from agentos_runner.opencode.session import OpenCodeModelSession
from agentos_runner.plugin import BundleInstaller, PluginBundleError

_INSTALLER_LOGGER = "agentos_runner.opencode.installer"

_HERE = Path(__file__).resolve().parent
_LIVE_BUNDLE = _HERE / "fixtures" / "opencode_live_bundle"
_PLUGIN_FORMAT_FIXTURES = _HERE.parents[1] / "packages/plugin-format/tests/fixtures"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_manifest(root: Path, name: str = "synthetic-bundle", **extra: object) -> None:
    manifest: dict[str, object] = {"name": name}
    manifest.update(extra)
    _write(root / ".claude-plugin" / "plugin.json", json.dumps(manifest))


def _relfiles(workdir: str) -> set[str]:
    base = Path(workdir)
    return {p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file()}


def _frontmatter(text: str) -> dict:
    assert text.startswith("---"), "compiled markdown should retain a frontmatter block"
    _, block, _body = text.split("---", 2)
    loaded = yaml.safe_load(block)
    return loaded or {}


def _opencode_config(workdir: str) -> dict:
    return json.loads((Path(workdir) / "opencode.json").read_text(encoding="utf-8"))


def _messages(caplog) -> str:
    return " ".join(record.getMessage() for record in caplog.records)


# 1 -------------------------------------------------------------------------
def test_install_none_returns_none_without_effect(tmp_path, caplog) -> None:
    installer = OpenCodeBundleInstaller(workdir_root=str(tmp_path))
    with caplog.at_level(logging.WARNING, logger=_INSTALLER_LOGGER):
        assert installer.install(None) is None
        assert installer.install("") is None
    assert caplog.records == []
    assert list(tmp_path.iterdir()) == []  # no filesystem effect for a bundle-less install


# 2 -------------------------------------------------------------------------
def test_installer_satisfies_bundle_installer_port() -> None:
    assert isinstance(OpenCodeBundleInstaller(), BundleInstaller)


# 3 -------------------------------------------------------------------------
def test_invalid_bundle_raises_naming_issue(tmp_path) -> None:
    installer = OpenCodeBundleInstaller(workdir_root=str(tmp_path))
    bad = _PLUGIN_FORMAT_FIXTURES / "bad_manifest_name"
    with pytest.raises(PluginBundleError) as exc:
        installer.install(str(bad))
    # Parity with load_plugins: the aggregated validation issue code surfaces.
    assert "manifest.name_invalid" in str(exc.value)
    assert list(tmp_path.iterdir()) == []  # a rejected bundle materializes nothing


# 4 -------------------------------------------------------------------------
def test_full_fixture_compile_materializes_expected_workdir(tmp_path) -> None:
    bundle = str(_LIVE_BUNDLE)
    compiled = OpenCodeBundleInstaller(workdir_root=str(tmp_path)).install(bundle)
    assert isinstance(compiled, CompiledOpenCodeBundle)
    workdir = Path(compiled.workdir)

    # Exactly these files land; mcp/ probe server is referenced in place, never copied.
    assert _relfiles(compiled.workdir) == {
        "opencode.json",
        ".claude/skills/roundtrip-greeter/SKILL.md",
        ".opencode/commands/ping.md",
    }

    # Skill copies byte-identical.
    source = (_LIVE_BUNDLE / "skills/roundtrip-greeter/SKILL.md").read_bytes()
    assert (workdir / ".claude/skills/roundtrip-greeter/SKILL.md").read_bytes() == source

    config = _opencode_config(compiled.workdir)
    assert config["$schema"] == "https://opencode.ai/config.json"
    probe = config["mcp"]["probe"]
    assert probe["type"] == "local"
    # ${CLAUDE_PLUGIN_ROOT} expands to the absolute bundle path; command becomes an array.
    assert probe["command"] == ["python3", f"{bundle}/mcp/mcp_probe_server.py"]
    assert probe["environment"] == {"PROBE_MODE": "1"}
    # stdio server cwd pinned to the bundle dir so relative paths behave (plan 8.4).
    assert probe["cwd"] == bundle


# 5 -------------------------------------------------------------------------
def test_remote_server_remap(tmp_path) -> None:
    bundle = tmp_path / "bundle"
    _write_manifest(bundle, name="remote-bundle", mcpServers=".mcp.json")
    _write(
        bundle / ".mcp.json",
        json.dumps(
            {
                "mcpServers": {
                    "remote-api": {
                        "type": "http",
                        "url": "https://mcp.example.com/v1",
                        "headers": {"X-Api-Style": "static"},
                    }
                }
            }
        ),
    )
    compiled = OpenCodeBundleInstaller(workdir_root=str(tmp_path)).install(str(bundle))
    remote = _opencode_config(compiled.workdir)["mcp"]["remote-api"]
    assert remote["type"] == "remote"  # http/sse transport distinction collapses to remote
    assert remote["url"] == "https://mcp.example.com/v1"
    assert remote["headers"] == {"X-Api-Style": "static"}


# 6 -------------------------------------------------------------------------
def test_skill_copy_fidelity_and_warnings(tmp_path, caplog) -> None:
    installer = OpenCodeBundleInstaller(workdir_root=str(tmp_path))
    with caplog.at_level(logging.WARNING, logger=_INSTALLER_LOGGER):
        compiled = installer.install(str(_LIVE_BUNDLE))

    source = (_LIVE_BUNDLE / "skills/roundtrip-greeter/SKILL.md").read_bytes()
    dest = (Path(compiled.workdir) / ".claude/skills/roundtrip-greeter/SKILL.md").read_bytes()
    assert dest == source  # verbatim copy including the ignored allowed-tools key

    warned = _messages(caplog)
    assert "allowed-tools" in warned
    assert "roundtrip-greeter" in warned  # the fidelity warning names the affected skill

    # A frontmatter name that passes the lenient validator but violates OpenCode's
    # kebab pattern is copied but warned (OpenCode would silently not discover it).
    caplog.clear()
    kebab = tmp_path / "kebab"
    _write_manifest(kebab, name="kebab-bundle")
    _write(
        kebab / "skills" / "greeter" / "SKILL.md",
        "---\nname: Greeter_Caps\ndescription: Not kebab-case.\n---\n\nHello.\n",
    )
    with caplog.at_level(logging.WARNING, logger=_INSTALLER_LOGGER):
        installer.install(str(kebab))
    assert "Greeter_Caps" in _messages(caplog)


# 7 -------------------------------------------------------------------------
def test_mcp_inline_dict_and_path_pointer_forms(tmp_path) -> None:
    installer = OpenCodeBundleInstaller(workdir_root=str(tmp_path))

    # Inline-object manifest mcpServers form.
    inline = tmp_path / "inline"
    _write_manifest(
        inline,
        name="inline-bundle",
        mcpServers={"inline-srv": {"command": "python3", "args": ["-m", "srv"]}},
    )
    inline_mcp = _opencode_config(installer.install(str(inline)).workdir)["mcp"]
    assert inline_mcp["inline-srv"]["type"] == "local"
    assert inline_mcp["inline-srv"]["command"] == ["python3", "-m", "srv"]

    # Path-pointer manifest form (valid_bundle points mcpServers at ".mcp.json"):
    # each server appears exactly once, not duplicated by re-reading root .mcp.json.
    valid = _PLUGIN_FORMAT_FIXTURES / "valid_bundle"
    valid_mcp = _opencode_config(installer.install(str(valid)).workdir)["mcp"]
    assert set(valid_mcp) == {"local-tools", "remote-api"}
    assert valid_mcp["local-tools"]["command"] == ["python", "-m", "demo_tools"]
    assert valid_mcp["local-tools"]["environment"] == {"DEMO_MODE": "1"}
    assert valid_mcp["remote-api"]["type"] == "remote"


# 8 -------------------------------------------------------------------------
def test_duplicate_server_name_root_wins_and_warns(tmp_path, caplog) -> None:
    bundle = tmp_path / "bundle"
    _write_manifest(
        bundle,
        name="dup-bundle",
        mcpServers={"dup": {"command": "python3", "args": ["manifest_version.py"]}},
    )
    _write(
        bundle / ".mcp.json",
        json.dumps({"mcpServers": {"dup": {"command": "python3", "args": ["root_version.py"]}}}),
    )
    installer = OpenCodeBundleInstaller(workdir_root=str(tmp_path))
    with caplog.at_level(logging.WARNING, logger=_INSTALLER_LOGGER):
        compiled = installer.install(str(bundle))
    dup = _opencode_config(compiled.workdir)["mcp"]["dup"]
    assert dup["command"] == ["python3", "root_version.py"]  # root .mcp.json wins the collision
    assert "dup" in _messages(caplog)


# 9 -------------------------------------------------------------------------
def test_command_and_agent_frontmatter_stripping(tmp_path, caplog) -> None:
    bundle = tmp_path / "bundle"
    _write_manifest(bundle, name="cmd-agent-bundle")
    _write(
        bundle / "commands" / "deploy.md",
        "---\ndescription: Deploy the thing.\nmodel: sonnet\nargument-hint: <env>\n---\n\n"
        "Deploy to $ARGUMENTS now.\n",
    )
    _write(
        bundle / "agents" / "helper.md",
        "---\nname: helper\ndescription: A helper agent.\nmodel: opus\n"
        "tools:\n  - Bash\n  - Read\n---\n\nYou are a helper.\n",
    )
    installer = OpenCodeBundleInstaller(workdir_root=str(tmp_path))
    with caplog.at_level(logging.WARNING, logger=_INSTALLER_LOGGER):
        compiled = installer.install(str(bundle))
    workdir = Path(compiled.workdir)

    command_text = (workdir / ".opencode/commands/deploy.md").read_text(encoding="utf-8")
    command_fm = _frontmatter(command_text)
    assert "model" not in command_fm  # Claude model alias stripped (won't resolve in OpenCode)
    assert command_fm["description"] == "Deploy the thing."
    assert "Deploy to $ARGUMENTS now." in command_text  # body preserved intact

    agent_text = (workdir / ".opencode/agents/helper.md").read_text(encoding="utf-8")
    agent_fm = _frontmatter(agent_text)
    assert "model" not in agent_fm
    assert "tools" not in agent_fm  # Claude PascalCase tool list dropped
    assert agent_fm["description"] == "A helper agent."
    assert "You are a helper." in agent_text

    assert "model" in _messages(caplog).lower()


# 10 ------------------------------------------------------------------------
def test_scripts_and_hooks_warn_and_materialize_nothing(tmp_path, caplog) -> None:
    bundle = tmp_path / "bundle"
    _write_manifest(bundle, name="scripts-hooks-bundle", hooks={"PreToolUse": [{"matcher": "*"}]})
    _write(bundle / "scripts" / "setup.sh", "#!/bin/bash\necho hi\n")
    _write(
        bundle / "skills" / "s" / "SKILL.md",
        "---\nname: s\ndescription: A minimal skill so the bundle carries content.\n---\n\nBody.\n",
    )
    installer = OpenCodeBundleInstaller(workdir_root=str(tmp_path))
    with caplog.at_level(logging.WARNING, logger=_INSTALLER_LOGGER):
        compiled = installer.install(str(bundle))
    workdir = Path(compiled.workdir)

    files = _relfiles(compiled.workdir)
    assert not any("setup.sh" in f for f in files)  # scripts unsupported, not copied
    assert not (workdir / "scripts").exists()
    assert not any("hook" in f.lower() for f in files)  # hooks unsupported, not materialized

    warned = _messages(caplog).lower()
    assert "scripts" in warned
    assert "hook" in warned


# 11 ------------------------------------------------------------------------
def test_non_plugin_root_var_passes_through_and_warns(tmp_path, caplog) -> None:
    bundle = tmp_path / "bundle"
    _write_manifest(bundle, name="var-bundle", mcpServers=".mcp.json")
    _write(
        bundle / ".mcp.json",
        json.dumps(
            {
                "mcpServers": {
                    "envy": {
                        "command": "python3",
                        "args": ["-m", "srv"],
                        "env": {"UPSTREAM_TOKEN": "${MY_UPSTREAM_VAR}"},
                    }
                }
            }
        ),
    )
    installer = OpenCodeBundleInstaller(workdir_root=str(tmp_path))
    with caplog.at_level(logging.WARNING, logger=_INSTALLER_LOGGER):
        compiled = installer.install(str(bundle))
    environment = _opencode_config(compiled.workdir)["mcp"]["envy"]["environment"]
    # A ${VAR} other than CLAUDE_PLUGIN_ROOT is left verbatim (ADR-0009 owns delivery).
    assert environment == {"UPSTREAM_TOKEN": "${MY_UPSTREAM_VAR}"}
    assert "MY_UPSTREAM_VAR" in _messages(caplog)


# 12 ------------------------------------------------------------------------
def test_skills_only_bundle_omits_mcp_key(tmp_path) -> None:
    bundle = tmp_path / "bundle"
    _write_manifest(bundle, name="skills-only-bundle")
    _write(
        bundle / "skills" / "solo" / "SKILL.md",
        "---\nname: solo\ndescription: The only skill; no MCP anywhere.\n---\n\nBody.\n",
    )
    compiled = OpenCodeBundleInstaller(workdir_root=str(tmp_path)).install(str(bundle))
    workdir = Path(compiled.workdir)
    config = _opencode_config(compiled.workdir)
    assert "mcp" not in config  # pinned choice: the mcp key is ABSENT, not an empty object
    assert config["$schema"] == "https://opencode.ai/config.json"
    assert (workdir / ".claude/skills/solo/SKILL.md").is_file()


# 13 ------------------------------------------------------------------------
def test_symlink_in_skills_skipped_and_warns(tmp_path, caplog) -> None:
    outside = tmp_path / "outside_target.txt"
    outside.write_text("LEAKED-HOST-CONTENT", encoding="utf-8")

    bundle = tmp_path / "bundle"
    _write_manifest(bundle, name="symlink-bundle")
    skill_dir = bundle / "skills" / "realskill"
    _write(
        skill_dir / "SKILL.md",
        "---\nname: realskill\ndescription: A real skill next to a symlink.\n---\n\nBody.\n",
    )
    (skill_dir / "leak.txt").symlink_to(outside)

    installer = OpenCodeBundleInstaller(workdir_root=str(tmp_path))
    with caplog.at_level(logging.WARNING, logger=_INSTALLER_LOGGER):
        compiled = installer.install(str(bundle))
    workdir = Path(compiled.workdir)

    assert (workdir / ".claude/skills/realskill/SKILL.md").is_file()
    assert not (workdir / ".claude/skills/realskill/leak.txt").exists()  # symlink skipped
    for materialized in workdir.rglob("*"):
        if materialized.is_file():
            body = materialized.read_text(encoding="utf-8", errors="ignore")
            assert "LEAKED-HOST-CONTENT" not in body  # smuggled content never lands

    warned = _messages(caplog)
    assert "leak.txt" in warned or "symlink" in warned.lower()


# 14 ------------------------------------------------------------------------
def test_build_runner_binds_compiled_workdir_as_session_cwd() -> None:
    # Wiring: _build_runner compiles the bundle and binds the compiled workdir as
    # the session factory's cwd. Reading the private _cwd avoids widening session.py
    # public surface purely for a test.
    runner = _build_runner(str(_LIVE_BUNDLE))
    session = runner._factory()
    assert session._cwd is not None
    assert (Path(session._cwd) / "opencode.json").is_file()

    bundleless = _build_runner(None)
    assert bundleless._factory()._cwd is None


def test_build_runner_defaults_max_turns_to_20_like_claude_path(monkeypatch) -> None:
    # Parity: with AGENTOS_MAX_TURNS unset the OpenCode conformance runner must
    # bind the same default turn cap (20) that RunnerConfig.from_env applies to
    # the Claude path, not an unbounded None.
    monkeypatch.delenv("AGENTOS_MAX_TURNS", raising=False)
    session = cast(OpenCodeModelSession, _build_runner(None)._factory())
    assert session._max_turns == 20

    monkeypatch.setenv("AGENTOS_MAX_TURNS", "5")
    capped = cast(OpenCodeModelSession, _build_runner(None)._factory())
    assert capped._max_turns == 5


def test_build_runner_reuses_one_conformance_workdir_root() -> None:
    first_session = cast(OpenCodeModelSession, _build_runner(str(_LIVE_BUNDLE))._factory())
    second_session = cast(OpenCodeModelSession, _build_runner(str(_LIVE_BUNDLE))._factory())
    first = first_session._cwd
    second = second_session._cwd

    assert first is not None
    assert second is not None
    assert Path(first).parent == Path(second).parent
    assert Path(first).parent.name.startswith("agentos-opencode-conformance-")


def test_bundle_demo_gate_requires_live_skill_and_mcp_proof() -> None:
    valid = [
        json.dumps({"type": "text_delta", "text": "working"}),
        json.dumps({"type": "tool_note", "text": "loaded", "tool": "skill"}),
        json.dumps(
            {
                "type": "final",
                "text": f"{_BUNDLE_SKILL_TOKEN} {_BUNDLE_MCP_NONCE}",
                "status": "done",
            }
        )
        + "  ",
    ]

    assert _bundle_demo_failures(valid) == []
    assert _bundle_demo_failures([*valid, "not json"]) == ["no final/done"]

    wrong_tool = valid.copy()
    wrong_tool[1] = json.dumps({"type": "tool_note", "text": "loaded", "tool": "read"})
    assert _bundle_demo_failures(wrong_tool) == ["no skill tool_note"]

    missing_proof = valid.copy()
    missing_proof[-1] = json.dumps({"type": "final", "text": "other", "status": "done"})
    assert _bundle_demo_failures(missing_proof) == ["missing skill token", "missing nonce"]


# 15 ------------------------------------------------------------------------
def test_absolute_manifest_command_path_skipped_and_warns(tmp_path, caplog) -> None:
    # A manifest command pointer that is an absolute path replaces the join root
    # entirely (``root / "/abs"`` == ``/abs``); it must never be read.
    outside = tmp_path / "outside_abs.md"
    outside.write_text(
        "---\ndescription: Off-bundle command.\n---\n\nLEAKED-ABS-COMMAND\n",
        encoding="utf-8",
    )
    bundle = tmp_path / "bundle"
    _write_manifest(bundle, name="abs-cmd-bundle", commands=str(outside))
    _write(
        bundle / "skills" / "s" / "SKILL.md",
        "---\nname: s\ndescription: Content so the bundle is non-empty.\n---\n\nBody.\n",
    )
    installer = OpenCodeBundleInstaller(workdir_root=str(tmp_path))
    with caplog.at_level(logging.WARNING, logger=_INSTALLER_LOGGER):
        compiled = installer.install(str(bundle))

    files = _relfiles(compiled.workdir)
    assert not any(f.startswith(".opencode/commands/") for f in files)  # nothing materialized
    for rel in files:
        body = (Path(compiled.workdir) / rel).read_text(encoding="utf-8", errors="ignore")
        assert "LEAKED-ABS-COMMAND" not in body  # off-bundle content never lands
    warned = _messages(caplog)
    assert str(outside) in warned  # the warning names the declared path


# 16 ------------------------------------------------------------------------
def test_parent_traversal_manifest_agent_path_skipped_and_warns(tmp_path, caplog) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text(
        "---\ndescription: Off-bundle agent.\n---\n\nLEAKED-TRAVERSAL-AGENT\n",
        encoding="utf-8",
    )
    bundle = tmp_path / "bundle"
    _write_manifest(bundle, name="traversal-agent-bundle", agents="../outside.md")
    _write(
        bundle / "skills" / "s" / "SKILL.md",
        "---\nname: s\ndescription: Content so the bundle is non-empty.\n---\n\nBody.\n",
    )
    installer = OpenCodeBundleInstaller(workdir_root=str(tmp_path))
    with caplog.at_level(logging.WARNING, logger=_INSTALLER_LOGGER):
        compiled = installer.install(str(bundle))

    files = _relfiles(compiled.workdir)
    assert not any(f.startswith(".opencode/agents/") for f in files)  # traversal skipped
    for rel in files:
        body = (Path(compiled.workdir) / rel).read_text(encoding="utf-8", errors="ignore")
        assert "LEAKED-TRAVERSAL-AGENT" not in body
    assert "../outside.md" in _messages(caplog)


# 17 ------------------------------------------------------------------------
def test_symlinked_top_level_skills_dir_skipped_and_warns(tmp_path, caplog) -> None:
    # ``is_dir()`` follows a symlink, so a symlinked skills/ would iterate the
    # target's real children and bypass the per-entry guard. The top-level dir
    # must be rejected before it is walked.
    external = tmp_path / "external_skills"
    _write(
        external / "sneaky" / "SKILL.md",
        "---\nname: sneaky\ndescription: Off-bundle skill.\n---\n\nLEAKED-SYMLINK-SKILL\n",
    )
    bundle = tmp_path / "bundle"
    _write_manifest(bundle, name="symlink-skills-bundle")
    _write(
        bundle / "commands" / "ok.md",
        "---\ndescription: A real in-bundle command.\n---\n\nRun $ARGUMENTS.\n",
    )
    (bundle / "skills").symlink_to(external, target_is_directory=True)

    installer = OpenCodeBundleInstaller(workdir_root=str(tmp_path))
    with caplog.at_level(logging.WARNING, logger=_INSTALLER_LOGGER):
        compiled = installer.install(str(bundle))

    files = _relfiles(compiled.workdir)
    assert not any(".claude/skills" in f for f in files)  # symlinked skills/ not walked
    assert ".opencode/commands/ok.md" in files  # the real in-bundle content still compiles
    for rel in files:
        body = (Path(compiled.workdir) / rel).read_text(encoding="utf-8", errors="ignore")
        assert "LEAKED-SYMLINK-SKILL" not in body
    assert "skills" in _messages(caplog).lower()


# 18 ------------------------------------------------------------------------
def test_mcp_pointer_escaping_root_skipped_and_warns(tmp_path, caplog) -> None:
    outside_mcp = tmp_path / "outside_mcp.json"
    outside_mcp.write_text(
        json.dumps({"mcpServers": {"escapee": {"command": "python3", "args": ["x.py"]}}}),
        encoding="utf-8",
    )
    bundle = tmp_path / "bundle"
    _write_manifest(bundle, name="escape-mcp-bundle", mcpServers="../outside_mcp.json")
    _write(
        bundle / "skills" / "s" / "SKILL.md",
        "---\nname: s\ndescription: Content so the bundle is non-empty.\n---\n\nBody.\n",
    )
    installer = OpenCodeBundleInstaller(workdir_root=str(tmp_path))
    with caplog.at_level(logging.WARNING, logger=_INSTALLER_LOGGER):
        compiled = installer.install(str(bundle))

    config = _opencode_config(compiled.workdir)
    assert "mcp" not in config  # no server pulled in from outside the bundle root
    assert "../outside_mcp.json" in _messages(caplog)


# 19 ------------------------------------------------------------------------
def test_directory_form_manifest_command_pointer_collects_md(tmp_path, caplog) -> None:
    bundle = tmp_path / "bundle"
    _write_manifest(bundle, name="dir-cmd-bundle", commands="extra_cmds")
    _write(
        bundle / "extra_cmds" / "alpha.md",
        "---\ndescription: Alpha command.\n---\n\nAlpha runs $ARGUMENTS.\n",
    )
    _write(
        bundle / "extra_cmds" / "beta.md",
        "---\ndescription: Beta command.\n---\n\nBeta runs $ARGUMENTS.\n",
    )
    installer = OpenCodeBundleInstaller(workdir_root=str(tmp_path))
    with caplog.at_level(logging.WARNING, logger=_INSTALLER_LOGGER):
        compiled = installer.install(str(bundle))
    workdir = Path(compiled.workdir)

    files = _relfiles(compiled.workdir)
    assert ".opencode/commands/alpha.md" in files
    assert ".opencode/commands/beta.md" in files
    alpha = (workdir / ".opencode/commands/alpha.md").read_text(encoding="utf-8")
    assert "Alpha runs $ARGUMENTS." in alpha  # directory-form *.md content materializes


# 20 ------------------------------------------------------------------------
def test_deep_nested_skill_md_copied_with_warning(tmp_path, caplog) -> None:
    bundle = tmp_path / "bundle"
    _write_manifest(bundle, name="deep-skill-bundle")
    _write(
        bundle / "skills" / "mygroup" / "nested" / "SKILL.md",
        "---\nname: deep-skill\ndescription: Nested deeper than convention.\n---\n\nBody.\n",
    )
    installer = OpenCodeBundleInstaller(workdir_root=str(tmp_path))
    with caplog.at_level(logging.WARNING, logger=_INSTALLER_LOGGER):
        compiled = installer.install(str(bundle))
    workdir = Path(compiled.workdir)

    # Copied byte-identical despite the non-canonical depth.
    assert (workdir / ".claude/skills/mygroup/nested/SKILL.md").is_file()
    warned = _messages(caplog)
    assert "mygroup/nested/SKILL.md" in warned
    assert "nested deeper" in warned.lower()
