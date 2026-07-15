# INTERFACE: Bundle format

> Part of the AgentOS swappable-seam catalog — see the [seam index](../../interfaces.md).
> **Kind:** CLEAN &nbsp;·&nbsp; **Implementations today:** 1 &nbsp;·&nbsp; **Swap-readiness grade:** not separately graded

**Kind legend:** CLEAN = a real `Protocol`/typed port class · SOFT = swap via env/URL/prefix/wire, no code interface · NONE = not built yet.

## The black line

The frozen bundle/plugin manifest format: the **Claude Code plugin shape verbatim**, a deliberate
distribution wedge. What is swappable is the harness that consumes a bundle; what stays fixed is
the shape a bundle must have to be accepted. The package does not invent format extensions —
compatibility with real Claude Code bundles is the whole point, so the models are lenient
(`extra="allow"`) rather than strict, and any bundle written for Claude Code validates unchanged.

## Current contract

`validate_bundle(path) -> ValidationResult` is the single entry point every deploy path calls
(`packages/plugin-format/src/plugin_format/validate.py:55`). It returns path-qualified issues
(codes like `manifest.missing`, `manifest.name_invalid`, `mcp.server_incomplete`) instead of
raising. The shapes it checks (`packages/plugin-format/src/plugin_format/models.py`):
`PluginManifest` (`.claude-plugin/plugin.json`, `:35`) with `name` required and optional
`version`, `description`, `author`, `commands`, `agents`, `hooks` (`:55`), `mcpServers` (`:56`);
`SkillFrontmatter` (`skills/**/SKILL.md`, `:59`) with `name`/`description` required and
`allowed_tools` aliased to the verbatim `allowed-tools` key (`:71`); `McpServer`/`McpConfig`
(`.mcp.json`, `:74`, `:94`) where the validator enforces each server define `command` (stdio) or
`url` (remote). A second consumer must accept exactly these shapes.

## Implementations today

One: the `plugin_format` package. Like `aci-protocol` it is tri-language and frozen — Pydantic
source of truth, committed JSON Schema, generated types — CI-guarded by
`packages/plugin-format/tests/test_schema_compat.py`. Unlike `aci-protocol` it carries no
`PROTOCOL_VERSION`; the format is pinned to the Claude Code shape and the models are lenient by
design so future Claude Code keys still validate.

## Known leakage

By intent, the entire format is Claude-Code-shaped — that is the wedge, not a leak. The `hooks`
field is no longer dead: as of #272 it is validated at deploy time (`HookMatcherConfig` /
`HookDefinition` in `models.py`, enforced by `_validate_hooks` in `validate.py`) and its
`PreToolUse` command hooks are consumed by the runner (`runner/src/agentos_runner/hooks.py`),
which translates them into SDK `HookMatcher` guardrails that run before a matching tool call (exit 0
allows, exit 2 denies). Command hooks are advisory-unless-exit-2 (fail-OPEN): only a clean exit 2
denies the call; any non-2 exit, a timeout (60s budget), or a spawn failure is treated as a
non-blocking hook error and the tool call proceeds -- so a command hook is not a fail-closed security
control, matching Claude Code convention for author-declared hooks. Only `PreToolUse` is wired today;
other hook events validate but are not yet consumed. Epic #30 continues to define the remaining authoring extensions (approval-policy and
trigger declarations) alongside this.

The bundle also carries two AgentOS authoring extensions that are **deploy-time validated** (shape
enforced, malformed declarations rejected) but whose **runtime consumption is a separate not-yet-built
seam**: `triggers` (a list of `cron`/`webhook` declarations for waking the agent beyond chat, #273/#270
— see the [triggers seam](../triggers/INTERFACE.md)) and `approvalPolicy` (`{gates: [{gate, route}]}`
approval declarations, #273). Their validators live alongside the others in `validate.py`
(`triggers.*` / `approval_policy.*` error codes); a future kernel/ingress consumer reads these same
declarations.

## Cross-links

- **Guide:** [workflow-agent-conversion.md](./workflow-agent-conversion.md) — converting an existing workflow agent (deterministic pipeline + LLM at the edges) onto a bundle end to end (#275).
- **Epic(s):** [#30](https://github.com/curie-eng/agentos/issues/30) — document the dead `hooks` field and new approval/trigger declarations: each field's meaning, validation contract, and runner consumption
- **Vision doc:** [architecture-vision.md](../../architecture-vision.md) — the plugin format is the distribution wedge; not one of the six swap-readiness Jobs
- **ADR(s):** [ADR-0005](../../adr/0005-claude-agent-sdk-adapter-and-frozen-aci.md) — freezes `plugin-format` (with `aci-protocol`) as an interface built first
