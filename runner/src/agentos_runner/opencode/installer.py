"""The OpenCode bundle compiler: validated plugin bundle -> OpenCode session workdir.

``OpenCodeBundleInstaller`` is the OpenCode harness's :class:`BundleInstaller`
(Decision 3 of the harness-neutral runner seams ADR, PR #306; issue #310). It
validates a mounted Claude Code plugin bundle with the frozen
``plugin_format.validate_bundle`` and *materializes* a temporary OpenCode session
workdir that ``OpenCodeModelSession`` runs ``opencode serve`` in. OpenCode
discovers all four config surfaces natively from that cwd:

* ``opencode.json``            -- MCP servers under the ``mcp`` key
* ``.claude/skills/<name>/``   -- skills (copied byte-identical)
* ``.opencode/commands/*.md``  -- slash commands
* ``.opencode/agents/*.md``    -- subagents

A ``None``/empty ``plugin_dir`` returns ``None`` (no bundle configured, no
filesystem effect); an invalid bundle raises :class:`PluginBundleError` with the
aggregated validation issues, exactly as ``load_plugins`` does for the Claude
harness -- a broken bundle fails runner startup visibly rather than running
capability-less.

Fidelity record (AC2)
=====================

Compilation copies verbatim wherever OpenCode's leniency permits and transforms
only where the two formats' shapes genuinely differ. Every loss below is also
emitted at runtime as a ``logger.warning`` on ``agentos_runner.opencode.installer``
so an operator sees exactly what degraded; warnings never fail the install.

* **Skills** copy byte-identical into ``.claude/skills/``. OpenCode ignores
  unknown skill frontmatter, so a skill's ``allowed-tools`` restriction is
  **lost** (per-skill tool limits are not enforced; warned per skill). OpenCode
  discovers a skill only when its frontmatter ``name`` is kebab-case
  (``^[a-z0-9]+(-[a-z0-9]+)*$``); a validator-passing but non-kebab name is
  copied yet **silently undiscovered** by OpenCode (warned). A ``SKILL.md``
  nested deeper than ``skills/<name>/SKILL.md`` is copied but **not discovered**
  by OpenCode (warned). Symlinks inside ``skills/`` -- and a symlinked or
  out-of-root ``skills/`` directory itself -- are **skipped** with a warning
  (never followed) so an out-of-bundle target cannot leak host/container content
  into the model-readable workdir.
* **MCP servers** (from root ``.mcp.json`` and/or the manifest ``mcpServers``
  field, inline object or path string) remap into ``opencode.json``. A stdio
  ``{command, args, env}`` becomes ``{type: "local", command: [command, *args],
  environment: env, cwd: <bundle dir>}``. A remote ``{type: "http"|"sse", url,
  headers}`` becomes ``{type: "remote", url, headers}`` -- Claude's ``http`` vs
  ``sse`` transport distinction **collapses to** ``remote`` (SSE-transport
  servers are untested under OpenCode). ``${CLAUDE_PLUGIN_ROOT}`` anywhere in a
  command/args/env/url/headers value expands to the absolute bundle path (the one
  substitution knowable at compile time). Any other ``${VAR}`` **passes through
  verbatim** and unexpanded -- secret delivery is ADR-0009's seam, out of scope
  here (warned per occurrence). On a duplicate server name across declaration
  forms the **root ``.mcp.json`` entry wins** (warned).
* **Commands** copy into ``.opencode/commands/<name>.md`` with the shared
  ``$ARGUMENTS`` body intact. Sources come from the conventional ``commands/``
  directory and any manifest ``commands`` pointer (a file, or a directory whose
  ``*.md`` are collected). A manifest-declared path that resolves outside the
  bundle root, is a symlink, or matches nothing is **skipped** with a warning
  (same guards apply to ``agents``). A Claude ``model`` alias in the frontmatter
  will not resolve in OpenCode, so ``model`` is **stripped** (warned); other
  Claude-only frontmatter (``allowed-tools``, ``argument-hint``,
  ``disable-model-invocation``) is harmlessly ignored by OpenCode and kept.
* **Agents** copy into ``.opencode/agents/<name>.md`` with ``description`` (and
  everything else) kept but ``model`` and ``tools`` **stripped**: Claude model
  aliases do not resolve and the ``tools`` list names PascalCase Claude tools
  meaningless to OpenCode (OpenCode restricts via ``permission`` config instead;
  that restriction is **not** reconstructed here). Warned.
* **scripts/** and manifest **hooks** are **unsupported**: bundle scripts have no
  OpenCode target and Claude hook events have no OpenCode equivalent in this
  compiler (OpenCode's hook system is JS-native). Nothing is materialized for
  either; each is warned when present.
* **plugin.json metadata** (version/description/author/homepage/keywords) is
  **dropped** -- it has no OpenCode session surface; only ``name`` is used, for
  log context.

See ``runner/README.md`` for the operator-facing pointer to this record.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from plugin_format import McpConfig, McpServer, PluginManifest

from ..plugin import PluginBundleError, ensure_valid_bundle

logger = logging.getLogger(__name__)

_OPENCODE_SCHEMA = "https://opencode.ai/config.json"
_PLUGIN_ROOT_VAR = "CLAUDE_PLUGIN_ROOT"
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_KEBAB_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_MANIFEST_LOCATIONS = (Path(".claude-plugin") / "plugin.json", Path("plugin.json"))


@dataclass(frozen=True)
class CompiledOpenCodeBundle:
    """A materialized OpenCode session workdir.

    ``workdir`` is passed straight to ``OpenCodeModelSession(cwd=...)``; the
    frozen dataclass (not a bare ``str``) leaves room for #311 to add fields
    without changing the ``BundleInstaller`` port signature.
    """

    workdir: str


class OpenCodeBundleInstaller:
    """Compile a validated plugin bundle into an OpenCode session workdir."""

    def __init__(self, workdir_root: str | None = None) -> None:
        self._workdir_root = workdir_root

    def install(self, plugin_dir: str | None) -> CompiledOpenCodeBundle | None:
        if not plugin_dir:
            return None

        root = Path(plugin_dir).resolve()
        ensure_valid_bundle(root)

        plugin_root = str(root)
        manifest = _load_manifest(root)
        workdir = Path(
            tempfile.mkdtemp(prefix="agentos-opencode-bundle-", dir=self._workdir_root)
        )

        _compile_skills(root, workdir)
        _compile_commands(root, manifest, workdir)
        _compile_agents(root, manifest, workdir)
        mcp = _compile_mcp(root, manifest, plugin_root)
        _warn_unsupported(root, manifest)

        config: dict[str, Any] = {"$schema": _OPENCODE_SCHEMA}
        if mcp:
            config["mcp"] = mcp
        (workdir / "opencode.json").write_text(
            json.dumps(config, indent=2) + "\n", encoding="utf-8"
        )
        logger.info("compiled OpenCode bundle %r into %s", manifest.name, workdir)
        return CompiledOpenCodeBundle(workdir=str(workdir))


def _load_manifest(root: Path) -> PluginManifest:
    for loc in _MANIFEST_LOCATIONS:
        path = root / loc
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            return PluginManifest.model_validate(data)
    # Unreachable after a passing validate_bundle (manifest presence is checked
    # there); defensive so a caller cannot proceed without a manifest.
    raise PluginBundleError(f"no manifest found in validated bundle at {root}")


# -- skills -----------------------------------------------------------------


def _compile_skills(root: Path, workdir: Path) -> None:
    skills_dir = root / "skills"
    if not _walkable_top_dir(root, skills_dir, "skills"):
        return
    _copy_skill_tree(skills_dir, workdir / ".claude" / "skills", skills_dir)


def _copy_skill_tree(src: Path, dest: Path, skills_root: Path) -> None:
    """Copy ``src`` into ``dest`` byte-identical, skipping symlinks with a warning.

    Symlinks are checked at every level and never followed, so a symlinked
    directory cannot smuggle out-of-bundle content into the session workdir. A
    ``SKILL.md`` nested deeper than ``skills/<name>/SKILL.md`` is copied but
    warned (OpenCode will not discover it).
    """

    for entry in sorted(src.iterdir()):
        if entry.is_symlink():
            logger.warning(
                "skipping symlink %s under skills/: symlinks are not copied into "
                "the OpenCode session workdir (an out-of-bundle target would leak "
                "host content the model could read)",
                entry.name,
            )
            continue
        target = dest / entry.name
        if entry.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            _copy_skill_tree(entry, target, skills_root)
        elif entry.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(entry, target)
            if entry.name == "SKILL.md":
                rel = entry.relative_to(skills_root)
                if len(rel.parts) > 2:
                    logger.warning(
                        "skill file %s is nested deeper than skills/<name>/SKILL.md; "
                        "it is copied but OpenCode discovers skills only at "
                        "skills/<name>/SKILL.md, so this one stays undiscovered",
                        rel.as_posix(),
                    )
                _warn_skill_frontmatter(entry)


def _warn_skill_frontmatter(skill_file: Path) -> None:
    frontmatter = _parse_frontmatter(skill_file.read_text(encoding="utf-8"))
    if frontmatter is None:
        return
    name = frontmatter.get("name")
    if "allowed-tools" in frontmatter or "allowed_tools" in frontmatter:
        logger.warning(
            "skill %r declares 'allowed-tools'; OpenCode ignores unknown skill "
            "frontmatter, so its per-skill tool restriction is lost",
            name,
        )
    if isinstance(name, str) and not _KEBAB_RE.match(name):
        logger.warning(
            "skill name %r is not kebab-case (^[a-z0-9]+(-[a-z0-9]+)*$); OpenCode "
            "will silently not discover it",
            name,
        )


# -- commands & agents ------------------------------------------------------


def _compile_commands(root: Path, manifest: PluginManifest, workdir: Path) -> None:
    for src in _collect_markdown(root, "commands", manifest.commands):
        _emit_markdown(src, workdir / ".opencode" / "commands", {"model"}, "command")


def _compile_agents(root: Path, manifest: PluginManifest, workdir: Path) -> None:
    for src in _collect_markdown(root, "agents", manifest.agents):
        _emit_markdown(src, workdir / ".opencode" / "agents", {"model", "tools"}, "agent")


def _collect_markdown(
    root: Path, dirname: str, declared: str | list[str] | None
) -> list[Path]:
    """Collect markdown sources: conventional dir first, then manifest paths.

    Keyed by output filename so a later declaration of the same basename wins
    (warned); insertion order is preserved for a deterministic compile.
    """

    collected: dict[str, Path] = {}

    def add(path: Path) -> None:
        if path.name in collected:
            logger.warning(
                "%s %r declared more than once; the last declaration wins",
                dirname[:-1],
                path.name,
            )
        collected[path.name] = path

    conventional = root / dirname
    if _walkable_top_dir(root, conventional, dirname):
        _add_markdown_from_dir(conventional, dirname, add)

    declared_paths = [declared] if isinstance(declared, str) else (declared or [])
    for rel in declared_paths:
        _collect_declared_markdown(root, dirname, rel, add)

    return list(collected.values())


def _collect_declared_markdown(
    root: Path, dirname: str, rel: str, add: Callable[[Path], None]
) -> None:
    """Collect markdown from a manifest-declared ``commands``/``agents`` path.

    The path is bundle-supplied, so it is contained to the resolved bundle root
    before anything is read (see :func:`_contained_declared_path`). A contained
    directory contributes its ``*.md`` (same per-entry symlink guard); a path
    matching nothing is skipped with a warning.
    """

    path = _contained_declared_path(root, rel, dirname)
    if path is None:
        return
    if path.is_file():
        add(path)
    elif path.is_dir():
        _add_markdown_from_dir(path, rel, add)
    else:
        logger.warning(
            "%s path %r declared in the manifest matches no file or directory; "
            "nothing is materialized for it",
            dirname,
            rel,
        )


def _emit_markdown(src: Path, dest_dir: Path, strip: set[str], kind: str) -> None:
    text = src.read_text(encoding="utf-8")
    frontmatter = _parse_frontmatter(text)
    if frontmatter is None:
        out = text
    else:
        dropped = [key for key in strip if key in frontmatter]
        kept = {key: value for key, value in frontmatter.items() if key not in strip}
        body = text.split("---", 2)[2]
        out = _render_markdown(kept, body)
        for key in dropped:
            logger.warning(
                "%s %r: dropped frontmatter key %r (its Claude value does not "
                "resolve in OpenCode)",
                kind,
                src.name,
                key,
            )
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / src.name).write_text(out, encoding="utf-8")


def _render_markdown(frontmatter: dict[str, Any], body: str) -> str:
    block = yaml.safe_dump(frontmatter, sort_keys=False) if frontmatter else ""
    return f"---\n{block}---{body}"


# -- MCP --------------------------------------------------------------------


def _compile_mcp(
    root: Path, manifest: PluginManifest, plugin_root: str
) -> dict[str, Any]:
    servers: dict[str, McpServer] = {}
    declared_file: Path | None = None

    declared = manifest.mcpServers
    if isinstance(declared, dict):
        servers.update(_parse_mcp_object(declared))
    elif isinstance(declared, str):
        declared_path = _contained_declared_path(root, declared, "mcpServers")
        if declared_path is not None and declared_path.is_file():
            servers.update(_parse_mcp_file(declared_path))
            declared_file = declared_path.resolve()

    root_mcp = root / ".mcp.json"
    if root_mcp.is_file() and root_mcp.resolve() != declared_file:
        for name, server in _parse_mcp_file(root_mcp).items():
            if name in servers:
                logger.warning(
                    "MCP server %r is declared in both the manifest and root "
                    ".mcp.json; the root .mcp.json entry wins",
                    name,
                )
            servers[name] = server  # root .mcp.json wins the collision

    return {name: _remap_server(server, plugin_root) for name, server in servers.items()}


def _parse_mcp_object(obj: object) -> dict[str, McpServer]:
    # Accept both a full config object and a bare servers map (the inline
    # manifest form), mirroring validate.py's _validate_mcp_object.
    payload = obj if isinstance(obj, dict) and "mcpServers" in obj else {"mcpServers": obj}
    return McpConfig.model_validate(payload).mcpServers


def _parse_mcp_file(path: Path) -> dict[str, McpServer]:
    return _parse_mcp_object(json.loads(path.read_text(encoding="utf-8")))


def _remap_server(server: McpServer, plugin_root: str) -> dict[str, Any]:
    if server.command is not None:
        command = [_expand(server.command, plugin_root)]
        command.extend(_expand(arg, plugin_root) for arg in server.args or [])
        local: dict[str, Any] = {"type": "local", "command": command}
        if server.env is not None:
            local["environment"] = {
                key: _expand(value, plugin_root) for key, value in server.env.items()
            }
        # Pin the stdio server's cwd to the bundle dir so relative paths behave
        # as they did under Claude's plugin-root semantics (plan 8.4).
        local["cwd"] = plugin_root
        return local

    remote: dict[str, Any] = {"type": "remote", "url": _expand(server.url or "", plugin_root)}
    if server.headers is not None:
        remote["headers"] = {
            key: _expand(value, plugin_root) for key, value in server.headers.items()
        }
    return remote


def _expand(value: str, plugin_root: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        var = match.group(1)
        if var == _PLUGIN_ROOT_VAR:
            return plugin_root
        logger.warning(
            "MCP config references ${%s}; only ${CLAUDE_PLUGIN_ROOT} expands at "
            "compile time, so this passes through verbatim (secret delivery is "
            "ADR-0009, out of scope for the compiler)",
            var,
        )
        return match.group(0)

    return _VAR_RE.sub(_replace, value)


# -- unsupported surfaces ---------------------------------------------------


def _warn_unsupported(root: Path, manifest: PluginManifest) -> None:
    if (root / "scripts").exists():
        logger.warning(
            "bundle declares scripts/ but OpenCode has no target for bundle "
            "scripts; nothing is materialized for them"
        )
    if manifest.hooks is not None:
        logger.warning(
            "bundle manifest declares hooks but OpenCode's plugin/hook system is "
            "JS-native; no hooks are translated"
        )


# -- shared helpers ---------------------------------------------------------


def _within_root(root: Path, candidate: Path) -> bool:
    """Whether ``candidate`` resolves to a path inside the resolved bundle root.

    ``root`` is already resolved at :meth:`OpenCodeBundleInstaller.install`.
    Resolving the candidate collapses ``..`` traversal, absolute replacement
    (``root / "/abs"`` == ``/abs``), and symlink targets, so a bundle-supplied
    path pointing anywhere outside the bundle is caught here before it is read.
    """

    return candidate.resolve().is_relative_to(root)


def _contained_declared_path(root: Path, rel: str, label: str) -> Path | None:
    """Resolve a manifest-declared ``rel`` within ``root`` or warn and skip.

    The shared containment gate for every manifest pointer (``commands``,
    ``agents``, ``mcpServers``): a path resolving outside the resolved bundle
    root, or a symlink, is rejected with a warning naming ``rel`` and ``None`` is
    returned so the caller materializes nothing. Returns the joined path when it
    is contained, leaving file-vs-dir dispatch to the caller.
    """

    path = root / rel
    if not _within_root(root, path):
        logger.warning(
            "%s path %r declared in the manifest resolves outside the bundle root; "
            "skipping it (a manifest must not pull files in from outside the bundle)",
            label,
            rel,
        )
        return None
    if path.is_symlink():
        logger.warning(
            "%s path %r declared in the manifest is a symlink; skipping it "
            "(symlinks are never followed into the OpenCode session workdir)",
            label,
            rel,
        )
        return None
    return path


def _add_markdown_from_dir(
    directory: Path, label: str, add: Callable[[Path], None]
) -> None:
    """Add every direct ``*.md`` under ``directory``, skipping symlinks with a warning.

    ``label`` names the source (the conventional dirname or the declared rel) so
    the per-entry symlink warning points at what the operator actually wrote.
    """

    for md in sorted(directory.glob("*.md")):
        if md.is_symlink():
            logger.warning("skipping symlink %s under %s/", md.name, label)
            continue
        add(md)


def _walkable_top_dir(root: Path, path: Path, label: str) -> bool:
    """Whether a conventional top-level dir is safe to iterate.

    ``is_dir()`` follows symlinks, so a bundle shipping ``skills``/``commands``/
    ``agents`` as a symlink (or a path escaping the root) would have its target's
    real children iterated and the per-entry guards never fire. Skip such a dir
    with a warning; return ``False`` (no warning) when the path simply is not a
    directory.
    """

    if not path.exists():
        return False
    if path.is_symlink() or not _within_root(root, path):
        logger.warning(
            "skipping top-level %s/: it is a symlink or resolves outside the bundle "
            "root, so its contents are not materialized into the OpenCode session "
            "workdir (an out-of-bundle target would leak host content the model "
            "could read)",
            label,
        )
        return False
    return path.is_dir()


def _parse_frontmatter(text: str) -> dict[str, Any] | None:
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        loaded = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return None
    return loaded if isinstance(loaded, dict) else None
