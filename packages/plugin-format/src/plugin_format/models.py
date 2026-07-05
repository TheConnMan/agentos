"""Pydantic models for the Claude Code plugin bundle shape (verbatim).

Compatibility with the Claude Code plugin format is the distribution wedge, so
these models mirror the real shapes and are deliberately permissive: unknown
keys are allowed, not rejected, because real bundles carry fields this MVP does
not model (and future Claude Code versions will add more). The models capture
the fields the platform reads; validators in ``validate.py`` turn structural
problems into actionable errors.

Shapes covered:
    .claude-plugin/plugin.json   PluginManifest
    skills/**/SKILL.md           SkillFrontmatter (YAML frontmatter)
    .mcp.json                    McpConfig / McpServer
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Forward compatible: accept and keep unknown keys rather than failing a real
# bundle that carries fields we do not model yet.
_LENIENT = ConfigDict(extra="allow", populate_by_name=True)


class Author(BaseModel):
    """The plugin manifest author object (or it may be a plain string)."""

    model_config = _LENIENT

    name: str
    email: str | None = None
    url: str | None = None


class PluginManifest(BaseModel):
    """The `.claude-plugin/plugin.json` manifest.

    Only ``name`` is required by the Claude Code format; everything else is
    optional. ``commands``/``agents`` may be a single path or a list; ``hooks``
    and ``mcpServers`` may be a path string or an inline object.
    """

    model_config = _LENIENT

    name: str
    version: str | None = None
    description: str | None = None
    author: str | Author | None = None
    homepage: str | None = None
    repository: str | None = None
    license: str | None = None
    keywords: list[str] | None = None
    commands: str | list[str] | None = None
    agents: str | list[str] | None = None
    hooks: str | dict[str, Any] | None = None
    mcpServers: str | dict[str, Any] | None = None


class SkillFrontmatter(BaseModel):
    """The YAML frontmatter of a `skills/<name>/SKILL.md` file.

    ``name`` and ``description`` are required. The tool restriction field is the
    verbatim Claude Code key ``allowed-tools`` (see the package README Decisions
    for why this differs from the task's shorthand "tools").
    """

    model_config = _LENIENT

    name: str
    description: str
    allowed_tools: list[str] | None = Field(default=None, alias="allowed-tools")


class McpServer(BaseModel):
    """One entry in `.mcp.json` mcpServers.

    A single permissive model rather than a strict stdio-vs-remote union, to
    stay forward compatible with Claude Code's evolving server shapes. A stdio
    server carries ``command`` (plus optional ``args``/``env``); a remote server
    carries ``type`` and ``url``. The validator enforces that one of the two is
    present.
    """

    model_config = _LENIENT

    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    type: str | None = None
    url: str | None = None
    headers: dict[str, str] | None = None


class McpConfig(BaseModel):
    """The `.mcp.json` document."""

    model_config = _LENIENT

    mcpServers: dict[str, McpServer] = Field(default_factory=dict)
