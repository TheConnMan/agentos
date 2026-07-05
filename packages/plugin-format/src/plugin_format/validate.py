"""Validate a plugin bundle directory against the Claude Code plugin shape.

``validate_bundle(path)`` is the entry point task B2 calls before versioning and
storing a bundle. It returns a ValidationResult with actionable, path-qualified
errors instead of raising, so the caller can surface every problem at once.
"""

import json
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from .models import McpConfig, PluginManifest, SkillFrontmatter

# Claude Code plugin names are kebab-case: lowercase alphanumerics and hyphens.
_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# The manifest lives at .claude-plugin/plugin.json; a bare plugin.json at the
# bundle root is accepted as a fallback (see README Decisions).
_MANIFEST_LOCATIONS = (Path(".claude-plugin") / "plugin.json", Path("plugin.json"))


class ValidationIssue(BaseModel):
    code: str
    message: str
    location: str


class ValidationResult(BaseModel):
    valid: bool
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []


class _Collector:
    def __init__(self) -> None:
        self.errors: list[ValidationIssue] = []
        self.warnings: list[ValidationIssue] = []

    def error(self, code: str, message: str, location: str) -> None:
        self.errors.append(ValidationIssue(code=code, message=message, location=location))

    def warn(self, code: str, message: str, location: str) -> None:
        self.warnings.append(ValidationIssue(code=code, message=message, location=location))

    def result(self) -> ValidationResult:
        return ValidationResult(
            valid=not self.errors, errors=self.errors, warnings=self.warnings
        )


def validate_bundle(path: str | Path) -> ValidationResult:
    """Validate the plugin bundle at ``path`` and return a ValidationResult."""

    root = Path(path)
    c = _Collector()

    if not root.is_dir():
        c.error("bundle.missing", f"bundle path is not a directory: {root}", str(root))
        return c.result()

    manifest = _validate_manifest(root, c)
    if manifest is not None:
        _validate_skills(root, c)
        _validate_mcp(root, manifest, c)
        _validate_scripts(root, c)

    return c.result()


def _validate_manifest(root: Path, c: _Collector) -> PluginManifest | None:
    manifest_path = next(
        (root / loc for loc in _MANIFEST_LOCATIONS if (root / loc).is_file()), None
    )
    if manifest_path is None:
        c.error(
            "manifest.missing",
            "no plugin manifest found at .claude-plugin/plugin.json or plugin.json",
            str(root),
        )
        return None

    rel = str(manifest_path.relative_to(root))
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        c.error("manifest.invalid_json", f"manifest is not valid JSON: {exc}", rel)
        return None

    try:
        manifest = PluginManifest.model_validate(data)
    except ValidationError as exc:
        for issue in _explain(exc):
            c.error("manifest.invalid", issue, rel)
        return None

    if not _NAME_RE.match(manifest.name):
        c.error(
            "manifest.name_invalid",
            f"plugin name {manifest.name!r} must be kebab-case "
            "(lowercase letters, digits, hyphens)",
            rel,
        )
    return manifest


def _validate_skills(root: Path, c: _Collector) -> None:
    skills_dir = root / "skills"
    if not skills_dir.is_dir():
        return

    skill_files = sorted(skills_dir.rglob("SKILL.md"))
    if not skill_files:
        c.warn("skills.empty", "skills/ exists but contains no SKILL.md files", "skills")
        return

    for skill_file in skill_files:
        rel = str(skill_file.relative_to(root))
        frontmatter = _read_frontmatter(skill_file, rel, c)
        if frontmatter is None:
            continue
        try:
            SkillFrontmatter.model_validate(frontmatter)
        except ValidationError as exc:
            for issue in _explain(exc):
                c.error("skill.frontmatter_invalid", issue, rel)


def _validate_mcp(root: Path, manifest: PluginManifest, c: _Collector) -> None:
    """Validate every MCP declaration: the manifest field and root .mcp.json.

    The manifest ``mcpServers`` may be an inline object or a path to a config
    file; either form is a supported declaration that must be checked, not only
    the conventional root ``.mcp.json``.
    """

    validated_files: set[Path] = set()
    declared = manifest.mcpServers

    if isinstance(declared, dict):
        _validate_mcp_object(declared, "plugin.json (mcpServers)", c)
    elif isinstance(declared, str):
        declared_path = root / declared
        if declared_path.is_file():
            _validate_mcp_file(declared_path, str(Path(declared)), c)
            validated_files.add(declared_path.resolve())
        else:
            c.error(
                "mcp.declared_missing",
                f"manifest mcpServers path {declared!r} was not found",
                "plugin.json",
            )

    root_mcp = root / ".mcp.json"
    if root_mcp.is_file() and root_mcp.resolve() not in validated_files:
        _validate_mcp_file(root_mcp, ".mcp.json", c)


def _validate_mcp_file(path: Path, location: str, c: _Collector) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        c.error("mcp.invalid_json", f"{location} is not valid JSON: {exc}", location)
        return
    _validate_mcp_object(data, location, c)


def _validate_mcp_object(obj: object, location: str, c: _Collector) -> None:
    # Accept both a full config object ({"mcpServers": {...}}) and a bare servers
    # map ({name: server}), which is how the manifest carries an inline value.
    payload = obj if isinstance(obj, dict) and "mcpServers" in obj else {"mcpServers": obj}
    try:
        config = McpConfig.model_validate(payload)
    except ValidationError as exc:
        for issue in _explain(exc):
            c.error("mcp.invalid", issue, location)
        return

    for name, server in config.mcpServers.items():
        if server.command is None and server.url is None:
            c.error(
                "mcp.server_incomplete",
                f"mcp server {name!r} must define either 'command' (stdio) or 'url' (remote)",
                location,
            )


def _validate_scripts(root: Path, c: _Collector) -> None:
    scripts = root / "scripts"
    if scripts.exists() and not scripts.is_dir():
        c.error("scripts.not_a_directory", "scripts must be a directory", "scripts")


def _read_frontmatter(skill_file: Path, rel: str, c: _Collector) -> dict[str, Any] | None:
    text = skill_file.read_text(encoding="utf-8")
    if not text.startswith("---"):
        c.error("skill.frontmatter_missing", "SKILL.md has no YAML frontmatter block", rel)
        return None

    parts = text.split("---", 2)
    if len(parts) < 3:
        c.error(
            "skill.frontmatter_unterminated",
            "SKILL.md frontmatter is not closed by '---'",
            rel,
        )
        return None

    try:
        loaded = yaml.safe_load(parts[1])
    except yaml.YAMLError as exc:
        c.error("skill.frontmatter_invalid_yaml", f"frontmatter is not valid YAML: {exc}", rel)
        return None

    if not isinstance(loaded, dict):
        c.error("skill.frontmatter_invalid", "frontmatter must be a YAML mapping", rel)
        return None
    return loaded


def _explain(exc: ValidationError) -> list[str]:
    out: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        out.append(f"{loc}: {err['msg']}")
    return out
