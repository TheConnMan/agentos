# Spike: plugin-bundle translator for a second (non-Claude-Code) harness

Date: 2026-07-10
Status: Spike report — scoping only, no commitment. Feeds the ADR-0011 gate.
Related: #25 (ACI second-harness epic), #257 (this spike), ADR-0011 (OpenCode as
second harness), ADR-0005 (frozen ACI + Claude-agent-sdk adapter).

> This is a scoping report, not a decision record. It sizes the translator work
> so a second-runner commitment is made with eyes open. It commits no code and
> no architecture; the binding decision lives in ADR-0011 and its steer gate.

## Question

The plugin bundle format is the Claude Code plugin shape **verbatim** — that is
the distribution wedge (`packages/plugin-format`, ADR-0005), and it is a frozen
contract we do not extend. A second harness (OpenCode is the ADR-0011 candidate)
does not consume Claude Code bundles natively. So a **translator** must map a
validated bundle into whatever the second engine reads at deploy/runtime time,
invisibly to the bundle author. ADR-0011 already asserts "the bundle translator,
not the ACI server, is the bulk of the work." This spike confronts that claim
concretely: enumerate what must be translated, size it, and give a go/no-go.

## What the source format actually is

The translatable surface is exactly what `plugin_format` models and
`validate_bundle` accepts (`packages/plugin-format/src/plugin_format/models.py`,
`validate.py`). It is small and, importantly, **lenient** (`extra="allow"`), so a
translator cannot assume the modeled fields are the whole payload — real bundles
carry unmodeled keys the platform preserves.

| Bundle element | Source location | Shape |
|---|---|---|
| Manifest | `.claude-plugin/plugin.json` (fallback: bare `plugin.json`) | `PluginManifest`: `name` required; optional `version`, `description`, `author`, `homepage`, `repository`, `license`, `keywords`, `commands`, `agents`, `hooks`, `mcpServers` |
| Skills | `skills/**/SKILL.md` | YAML frontmatter (`name`, `description`, optional `allowed-tools`) + markdown body |
| MCP servers | `.mcp.json` and/or manifest `mcpServers` (inline object or path) | `McpServer`: stdio (`command`,`args?`,`env?`) or remote (`type`,`url`,`headers?`) |
| Scripts | `scripts/` | directory convention, no schema |
| Commands / agents / hooks | manifest fields (path or inline) | Claude-Code-defined, **not** modeled here beyond "path string or object" |

## What a non-Claude-Code harness must translate

Grading each element by translation difficulty, using OpenCode as the concrete
target (per ADR-0011's docs review) but noting where the cost is engine-generic.

### 1. Skills — cheap, near-verbatim (LOW)

SKILL.md payloads port cleanly; ADR-0011 confirms OpenCode even reads the
`.claude/skills/` path. Translation is a path/frontmatter remap: `name`,
`description`, `allowed-tools`, markdown body carried through. The one real task
is the tool-name namespace: `allowed-tools` entries (`WebSearch`, `WebFetch`,
`Bash`, MCP tool refs) must map to the target engine's tool identifiers, and any
tool the target does not have must degrade to a documented no-op or hard error.
Body content is engine-neutral prose and needs no change.

### 2. MCP servers — moderate, mechanical (MEDIUM)

The blocker is purely structural, not semantic: OpenCode ignores Claude's
`.mcp.json` and manifest `mcpServers`; MCP must be emitted under OpenCode's own
`mcp` key. The stdio/remote distinction, `{env:VAR}` secret interpolation, and
OAuth are all first-class on the target (ADR-0011), so this is a key-rename +
reshape, not a reimplementation. Cost items: (a) resolve both source forms (root
`.mcp.json` **and** inline/path manifest `mcpServers`) into one set; (b) rewrite
env-secret references into the target's interpolation syntax; (c) preserve
unmodeled server keys rather than dropping them (lenient-model consequence).

### 3. Commands / agents / hooks — the real unknowns (HIGH / risk-bearing)

These are the fields the frozen format models only as "path or object" and does
**not** structurally validate. They are the bulk of the risk:

- **Hooks.** Claude Code hooks are event-keyed shell/command definitions. The
  target's hook model is different in surface and lifecycle (OpenCode ships a
  blocking `tool.execute.before` hook, not the full Claude hook matrix). A
  faithful translation requires a per-event mapping table and an explicit
  policy for unmappable events (drop-with-warning vs. fail-closed). This is
  where "faithful port" (memory: migration-fidelity) collides with "the target
  simply cannot express this."
- **Commands.** Slash-command definitions must map to the target's command
  concept or be synthesized as skills/prompts. Semantics (argument hints,
  frontmatter) are not modeled here, so the translator must parse the raw
  Claude Code command files itself — outside `plugin_format`'s guarantees.
- **Subagents.** OpenCode has subagents natively (ADR-0011), so `agents`
  entries are likely a reshape, but the Claude `agents` frontmatter is again
  unmodeled and must be parsed directly.

### 4. Leniency / unknown-key fidelity — cross-cutting (MEDIUM)

Because the source models are `extra="allow"`, the translator must decide, per
element, whether an unknown key is (a) safe to drop, (b) must be carried, or (c)
must fail translation. A silent drop of a key a future Claude Code version adds
is a correctness regression that `validate_bundle` will **not** catch (it passes
lenient bundles). The translator therefore needs its own "unrecognized field"
policy and test corpus, independent of the validator.

### 5. Steer — out of scope for the translator, but gates the whole effort

Not a translation task, but the standing risk from ADR-0011: mid-run steer is
not a shipped OpenCode primitive, and the worker's finish-race kernel invariant
assumes steer-at-a-tool-boundary. The translator can be perfect and the harness
still be only a degraded-steer implementation. **This gate dominates go/no-go and
is not retired by any translator work.**

## Effort estimate

Translator only (ACI server work and the steer spike are separate line items).
Ranges are engineer-weeks for one engineer, assuming `plugin_format` as the
trusted front door and a golden-bundle test corpus.

| Work item | Estimate | Confidence |
|---|---|---|
| Skills remap + tool-namespace mapping | 0.5–1 wk | high |
| MCP reshape (both source forms, env-secret rewrite, key preservation) | 1–1.5 wk | high |
| Commands/agents/hooks mapping + unmappable-event policy | 2–4 wk | **low** (the unknowns live here) |
| Unknown-key fidelity policy + differential test corpus | 1–1.5 wk | medium |
| Translator harness plumbing (deploy/runtime hook, no author-time change) | 1 wk | medium |
| **Total (translator alone)** | **~5.5–9 wks** | dominated by hooks/commands |

The spread is entirely in item 3: if the first agents we port use no hooks and
few custom commands (the weather example under `examples/` uses none), the low
end holds; a hook-heavy agent pushes to the high end and may surface an
unmappable event that forces a fidelity vs. capability decision.

## Go / no-go recommendation

**Conditional GO on building the translator — but sequence it behind the
ADR-0011 steer gate, and behind a corpus survey.** Rationale:

1. The translator is real, sizeable work (≈5.5–9 wks) and confirms ADR-0011's
   "translator is the bulk of the work" — but the cheap tiers (skills, MCP) are
   genuinely cheap and low-risk, and the expensive tier (hooks/commands) is only
   as expensive as the agents we actually intend to run demand.
2. **Do not start the translator until the steer spike resolves** (ADR-0011
   gate). A perfect translator on a degraded-steer harness may still be
   unacceptable against the finish-race kernel invariant; sinking translator
   weeks before that answer is premature.
3. **Before committing the HIGH tier, survey the real bundle corpus** for hook
   and custom-command usage. If the near-term agents are skills+MCP only, ship a
   thin translator (items 1–2 + 4, ≈3–4 wks) that fails closed on hooks/commands
   with a clear error, and defer item 3 until an agent needs it. This keeps the
   distribution wedge intact and avoids speculative mapping of Claude Code
   surfaces we do not yet run.

Net: the translator does not by itself sink the second-harness plan; the steer
gate does or does not. Recommend proceeding to the steer spike first, and
scoping the translator as thin-now / hooks-later against the actual agent corpus.

## Follow-ups (not committed here)

- ADR-0011 steer spike (the dominating gate) — must precede translator build.
- Bundle-corpus survey of hooks/commands/agents usage to fix item 3's scope.
- If GO: a differential test corpus (validated bundle → translated config →
  re-validate target) so leniency-driven key drops are caught, since
  `validate_bundle` will not catch them.
