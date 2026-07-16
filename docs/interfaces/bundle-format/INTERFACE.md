---
seam: Bundle format
kind: CLEAN, frozen
impls: "1"
grade: not separately graded
epics:
  - "#30"
order: 12
---

# INTERFACE: Bundle format

> Part of the AgentOS swappable-seam catalog — see the [seam index](../../interfaces.md).

<!-- BEGIN GENERATED: header (agentos dev docs-lint) -->
> **Kind:** CLEAN, frozen &nbsp;·&nbsp; **Implementations today:** 1 &nbsp;·&nbsp; **Swap-readiness grade:** not separately graded
<!-- END GENERATED: header -->

**Kind legend:** CLEAN = a real `Protocol`/typed port class · SOFT = swap via env/URL/prefix/wire, no code interface · NONE = not built yet.

## The black line

The frozen bundle/plugin manifest format: the **Claude Code plugin shape verbatim**, a deliberate
distribution wedge. What is swappable is the harness that consumes a bundle; what stays fixed is
the shape a bundle must have to be accepted. The base is the Claude Code plugin shape, and the
models are lenient (`extra="allow"`) rather than strict so any bundle written for Claude Code
validates unchanged. On top of that base the package **does add five AgentOS authoring
extensions** — `systemPrompt`, `starterPrompts`, `secrets`, `triggers`, `approvalPolicy` on
`packages/plugin-format/src/plugin_format/models.py::PluginManifest`, optional fields Claude Code
does not define. Leniency is what lets the Claude Code base and these extensions coexist; the
earlier "does not invent format extensions" framing was wrong.

## Current contract

`validate_bundle(path) -> ValidationResult` is the single entry point every deploy path calls
(`packages/plugin-format/src/plugin_format/validate.py::validate_bundle`). It returns path-qualified
issues (codes like `manifest.missing`, `manifest.name_invalid`, `mcp.server_incomplete`) instead of
raising. The shapes it checks (`packages/plugin-format/src/plugin_format/models.py`):
`PluginManifest` (`packages/plugin-format/src/plugin_format/models.py::PluginManifest`, the
`plugin.json` manifest) with `name` required and optional `version`, `description`, `author`,
`commands`, `agents`, `hooks`, `mcpServers`; `SkillFrontmatter`
(`packages/plugin-format/src/plugin_format/models.py::SkillFrontmatter`, a `SKILL.md` frontmatter)
with `name`/`description` required and `allowed_tools` aliased to the verbatim `allowed-tools` key;
`McpServer`/`McpConfig` (`packages/plugin-format/src/plugin_format/models.py::McpConfig`, the
`.mcp.json` file) where the validator enforces each server define `command` (stdio) or `url`
(remote). A second consumer must accept exactly these shapes.

The seam is bidirectional, and both directions are contracts. **Inbound**, leniency means any
bundle written for Claude Code validates here unchanged. **Outbound**, an AgentOS bundle must
validate unmodified as a Claude Code plugin — that direction is what makes the shape a
distribution wedge rather than a lookalike. The gate that defends it is
`scripts/check-plugin-compat.sh` (run it as `agentos dev plugin-compat`), which discovers every
bundle under `examples/` and asserts `claude plugin validate` exits 0 for each. CI runs the same
script from `.github/workflows/plugin-compat.yaml` on two triggers, because drift arrives from two
directions: a path-filtered `pull_request` trigger catches our own drift when we touch the bundles
or the format models, and a nightly `schedule` catches Claude Code changing the format under us,
which no PR of ours would ever surface. The check is deliberately not `--strict`: strict mode
promotes unknown-field warnings to errors, and the five AgentOS authoring extensions are
unknown-to-Claude-Code by design, so warnings are the expected steady state and only a non-zero
exit is a failure.

## Implementations today

One: the `plugin_format` package. **Unlike `aci-protocol`, it is NOT tri-language with generated
types.** The Pydantic models are the source of truth and a committed JSON Schema
(`packages/plugin-format/schema/plugin-format.schema.json`) is regenerated and drift-checked by
`packages/plugin-format/tests/test_schema_compat.py`, but there is **no generated Rust or TS** in
the package (contrast `packages/aci-protocol/generated/`): the Rust (CLI) and TypeScript (UI)
consumers of this format are **hand-written mirrors with no drift gate**, so a manifest field added
here does not fail CI if those mirrors fall behind. It also carries no `PROTOCOL_VERSION`; the
format is pinned to the Claude Code shape and the models are lenient by design so future Claude
Code keys still validate.

## Known leakage

By intent, the entire format is Claude-Code-shaped — that is the wedge, not a leak. What the wedge
costs is asymmetric fidelity: a bundle loaded by Claude Code **validates but degrades**. All five
AgentOS authoring extensions — `systemPrompt`, `starterPrompts`, `secrets`, `triggers`,
`approvalPolicy` — are unknown fields to Claude Code, which warns about each and then silently
ignores it at load time. The manifest is accepted and the commands, agents, hooks, and MCP servers
work; the agent's persona, its suggested openers, its secret declarations, its wake-up triggers,
and its approval gates do not travel. That degradation is by design (there is nowhere in the Claude
Code shape to put them), but it is silent from the operator's side, so it is documented here rather
than discovered. The `hooks`
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

Two of those AgentOS authoring extensions are **deploy-time validated** (shape enforced, malformed
declarations rejected), but they differ in whether the runtime acts on them yet:

- `approvalPolicy` (`{gates: [{gate, route}]}` approval declarations, #273) is **consumed at
  runtime**, not merely validated: the runner reads the gates at boot (`load_approval_policy`,
  #247 / ADR-0010) and arms each `{gate, route}` on the permission gate — so calling this
  "not-yet-built" is stale (see the [approval seam](../approval/INTERFACE.md)).
- `triggers` (a list of `cron`/`webhook` declarations for waking the agent beyond chat, #273/#270 —
  see the [triggers seam](../triggers/INTERFACE.md)) is still **declaration-only**: its validator
  runs at deploy, but no runtime scheduler/ingress consumes a declared trigger yet (Epic #29).

Their validators live alongside the others in `validate.py` (`triggers.*` / `approval_policy.*`
error codes).

## Cross-links

- **Guide:** [workflow-agent-conversion.md](./workflow-agent-conversion.md) — converting an existing workflow agent (deterministic pipeline + LLM at the edges) onto a bundle end to end (#275).
- **Epic(s):** [#30](https://github.com/curie-eng/agentos/issues/30) — document the dead `hooks` field and new approval/trigger declarations: each field's meaning, validation contract, and runner consumption
- **Vision doc:** [architecture-vision.md](../../architecture-vision.md) — the plugin format is the distribution wedge; not one of the six swap-readiness Jobs
- **ADR(s):** [ADR-0005](../../adr/0005-claude-agent-sdk-adapter-and-frozen-aci.md) — freezes `plugin-format` (with `aci-protocol`) as an interface built first
