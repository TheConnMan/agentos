import pytest
from plugin_format import McpServer, PluginManifest, SkillFrontmatter
from pydantic import ValidationError


def test_manifest_requires_name() -> None:
    with pytest.raises(ValidationError):
        PluginManifest.model_validate({"description": "no name"})


def test_manifest_accepts_and_keeps_unknown_keys() -> None:
    manifest = PluginManifest.model_validate({"name": "demo", "futureField": 42})
    assert manifest.name == "demo"
    assert manifest.model_dump().get("futureField") == 42


def test_manifest_author_may_be_string_or_object() -> None:
    assert PluginManifest.model_validate({"name": "d", "author": "Jane"}).author == "Jane"
    obj = PluginManifest.model_validate({"name": "d", "author": {"name": "Jane"}})
    assert obj.author.name == "Jane"  # type: ignore[union-attr]


def test_skill_frontmatter_alias_and_required_fields() -> None:
    fm = SkillFrontmatter.model_validate(
        {"name": "greeter", "description": "greets", "allowed-tools": ["Bash"]}
    )
    assert fm.allowed_tools == ["Bash"]

    with pytest.raises(ValidationError):
        SkillFrontmatter.model_validate({"name": "greeter"})


def test_mcp_server_accepts_stdio_and_remote_shapes() -> None:
    stdio = McpServer.model_validate({"command": "python", "args": ["-m", "x"]})
    remote = McpServer.model_validate({"type": "http", "url": "https://example.com"})
    assert stdio.command == "python"
    assert remote.url == "https://example.com"
