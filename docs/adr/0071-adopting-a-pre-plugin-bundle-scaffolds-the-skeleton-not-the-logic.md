# 71. Adopting a pre-plugin bundle scaffolds the skeleton, not the logic

Date: 2026-07-21

Status: Accepted

Sits alongside the `agentos init` scaffold (`cli/src/scaffold.rs`, which produces
the frozen `packages/plugin-format` shape verbatim) and ADR-0021 (the CLI is a
harness for coding agents). Supersedes nothing.

Closes the code half of [#745](https://github.com/curie-eng/agentos/issues/745)
("no adoption path for an existing non-plugin-shape bundle"), found in an
onboarding dogfood where "stand up the demo app on the skill tier" was not
completable because the agent already existed in the older `agent-ss-template`
shape.

## Context

`agentos skill up` requires a plugin bundle: `.claude-plugin/plugin.json`,
`skills/<name>/SKILL.md`, `.mcp.json`, `evals/cases.json`. An agent in the older
`agent-ss-template` shape — a Python package (`src/`, `Makefile`, `Dockerfile`,
`compose.yaml`, a Flask/Slack `server.py`) — has none of those, so `skill up`
dead-ends at `no plugin manifest under <dir>` with no path forward. Every
internal agent that predates the plugin format is in this position, so "point
AgentOS at the agent I already have" has no answer today. Our own cold-start
ladder never exercises this: every run begins with `agentos init <name>` against
a *freshly scaffolded* bundle, so the whole adopt-an-existing-bundle journey is a
structural blind spot.

Three options were weighed:

1. **A runner adapter** that consumes the old shape directly. Rejected: the old
   shape is a standalone Flask/Slack *server app*, a fundamentally different
   execution model from the claude-agent-sdk runner (skills + MCP). Running it
   natively means two agent runtimes in one image. The blessed way to reuse an
   existing engine is "engine as an in-bundle stdio MCP server" — which is
   epic #30's territory, not a #745 adoption path.
2. **A migration verb** that scaffolds the plugin skeleton around an existing
   tree.
3. **A documented hand-port guide.**

The load-bearing insight: the *skeleton* is mechanizable, but the *logic port* —
deciding what becomes a skill vs. an MCP tool vs. the system prompt — inherently
needs human judgment. No tool can auto-convert a Flask app into the plugin
format. So (2) and (3) are complements, not alternatives.

## Decision

Adopt options **2 + 3**, and reject option 1.

- **`agentos init --adopt <DIR>`** scaffolds the plugin skeleton *into* an
  existing directory: the same byte-compatible file set `init` already produces
  (`scaffold::scaffold`), with the plugin name derived from the directory's own
  name (kebab-sanitized; an explicit positional `NAME` overrides). It reuses the
  existing collision-refusal discipline, so it creates the plugin files
  *alongside* the existing `src/`/`Makefile`/etc. and never overwrites a file the
  operator already has. It does **not** touch or interpret the existing code —
  the logic port stays the operator's, guided by (3).
- **A porting guide** (`docs/adopting-a-bundle.md`) is the honest destination:
  it names the two shapes and walks the manual port (what maps to a skill, to an
  MCP server, to the system prompt).
- **The `no plugin manifest` error names the path forward.** When the directory
  looks like a pre-plugin bundle (a `src/` dir plus a `Makefile`/`Dockerfile`/
  `compose.yaml`/`server.py`), the error points at `init --adopt` and the guide
  instead of dead-ending.

## Consequences

- An operator with a pre-plugin agent runs `agentos init --adopt .`, gets a
  runnable plugin skeleton next to their code, and follows the guide to move the
  logic in — the dogfood blocker becomes a two-step on-ramp.
- The scaffold stays byte-identical to `init`'s (adopt reuses it verbatim), so
  the plugin-format byte-compat obligation and its tests are unchanged.
- The logic port is deliberately **not** automated; the guide sets that
  expectation rather than implying a magic converter.
- Deferred (out of scope, tracked under #745): the `revenue-leak-agent`
  README/COMMANDS docs that drive everything through `make`/`./rl` and never
  mention `agentos skill` — that doc fix lives in the agent's own repo.
