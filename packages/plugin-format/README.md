# plugin-format

Owning task: **C1**. The plugin bundle format: the Claude Code plugin shape
verbatim, plus its validator. Compatibility with the Claude Code plugin format
is the distribution wedge, so this package does not invent format extensions.

## Format surface

Pydantic models mirroring the Claude Code shapes:

- `PluginManifest` (`.claude-plugin/plugin.json`): `name` required; optional
  `version`, `description`, `author` (string or `{name, email?, url?}`),
  `homepage`, `repository`, `license`, `keywords`, `commands`, `agents`,
  `hooks`, `mcpServers`. Unknown keys are accepted and preserved.
- `SkillFrontmatter` (`skills/**/SKILL.md` YAML frontmatter): `name` and
  `description` required; `allowed-tools` optional.
- `McpServer` / `McpConfig` (`.mcp.json`): `mcpServers` maps a name to a server
  that is either stdio (`command`, `args?`, `env?`) or remote (`type`, `url`,
  `headers?`).
- `scripts/` is a directory convention (no manifest schema of its own).

`validate_bundle(path) -> ValidationResult` is the entry point B2 calls. It
returns actionable, path-qualified issues instead of raising:

```python
from plugin_format import validate_bundle
result = validate_bundle("path/to/bundle")
if not result.valid:
    for issue in result.errors:
        print(issue.code, issue.location, issue.message)
```

Error codes include `bundle.missing`, `manifest.missing`,
`manifest.invalid_json`, `manifest.invalid`, `manifest.name_invalid`,
`skill.frontmatter_missing`, `skill.frontmatter_invalid`, `mcp.invalid_json`,
`mcp.server_incomplete`, `scripts.not_a_directory`.

## Frozen-interface rule

This package is a **frozen interface** for the same reasons as `aci-protocol`:
compatibility is the wedge. Do not change it unilaterally; a needed change stops
the task and escalates to the orchestrator. Any change must regenerate the
committed schema with `scripts/check-contracts.sh` (which runs
`python -m plugin_format.schema_export`) and commit it. The compat gate
(`tests/test_schema_compat.py`) fails on drift.

## Decisions made under ambiguity

- **`allowed-tools`, not `tools`.** The task shorthand said SKILL.md frontmatter
  carries `name, description, tools`. The verbatim Claude Code / Agent Skills
  field is `allowed-tools`; using the real field name is the compatibility
  choice (the wedge), so the model exposes `allowed_tools` aliased to
  `allowed-tools`. A bundle written for Claude Code validates unchanged.
- **Lenient models (`extra="allow"`).** Real bundles and future Claude Code
  versions carry manifest and frontmatter keys this MVP does not model. Rejecting
  them would reject valid bundles, so the models accept and preserve unknown
  keys rather than forbidding them. (This is the opposite of `aci-protocol`,
  whose wire contract is strict.)
- **Manifest location.** The canonical location is `.claude-plugin/plugin.json`;
  a bare `plugin.json` at the bundle root is accepted as a fallback.
- **`McpServer` is one permissive model.** Rather than a strict stdio-vs-remote
  union, a single model with all fields optional stays forward compatible; the
  validator enforces that each server defines either `command` or `url`.
