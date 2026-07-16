---
seam: CLI output (agent-facing `--json`)
kind: CLEAN
impls: 9 outputs behind one trait
grade: not separately graded
epics:
  - "#456"
order: 18
---

# INTERFACE: CLI output (agent-facing `--json`)

> Part of the AgentOS swappable-seam catalog — see the [seam index](../../interfaces.md).

<!-- BEGIN GENERATED: header (agentos dev docs-lint) -->
> **Kind:** CLEAN &nbsp;·&nbsp; **Implementations today:** 9 outputs behind one trait &nbsp;·&nbsp; **Swap-readiness grade:** not separately graded
<!-- END GENERATED: header -->

**Kind legend:** CLEAN = a real `Protocol`/typed port class · SOFT = swap via env/URL/prefix/wire, no code interface · NONE = not built yet.

## The black line

The port is the `CliOutput` trait in `cli/src/ui.rs`. Two methods:

```rust
pub trait CliOutput {
    /// The single JSON object emitted under `--json`.
    fn to_json(&self) -> serde_json::Value;
    /// The human render (stdout payload lines) when not under `--json`.
    fn render(&self, ui: &Ui);
}
```

The swappable thing is **the rendering of a command's result**, not the command.
A verb computes a result value and hands it to `emit`; the machine-vs-human
decision is made in exactly one place (`Ui::emit`, `cli/src/ui.rs`) rather than at
every call site. This is the code-level enforcement of ADR-0021's first decision:
the CLI's primary user is a coding agent, so every verb must have a parseable
`--json` form and no verb may silently emit empty stdout under `--json`.

This is the catalog's first **Rust** seam. It is listed here because the agent-facing
`--json` contract is a public surface an agent branches on, the same way the ACI is.

## Current contract

- **One decision point.** `Ui::emit(&dyn CliOutput)` (`cli/src/ui.rs`) is the only
  success-path branch: under `--json` it writes `to_json()` as one compact line via
  `emit_json`; otherwise it calls `render(self)`. Handlers must not call a stdout
  emitter directly. `main`'s `emit<T: CliOutput>` helper (`cli/src/main.rs`) is the
  dispatch-side funnel that routes every read verb's return value through it.
- **One JSON object per invocation.** `to_json` returns a single
  `serde_json::Value`, emitted as one line. A multi-line or streamed stdout payload
  is outside this contract.
- **The error path is the mirror, not part of this trait.** Failures are emitted
  centrally by `main` and classified by `cli/src/exit.rs` into four stable exit codes
  agents branch on: `0` Success, `1` Failure, `2` Usage (deterministic input error;
  same argv fails identically), `3` Transient (retryable; dependency unreachable or
  timed out). `CliOutput` covers only exit-0 stdout.
- **Human and JSON render the same value.** Both methods read the same owned data,
  so the two paths cannot disagree about content — only about form. `VersionsOutput`
  (`cli/src/commands.rs`) documents this obligation explicitly: it holds versions
  newest-first, normalized once by the handler, because `to_json` and `render` each
  iterate it plainly and a constructor that broke the order would let the two paths
  silently diverge.

## Implementations today

Nine, all in the CLI crate:

- **`DryRunPlan`** (`cli/src/ui.rs`) — the generic `--dry-run` plan; JSON is
  `{"dry_run":true,"plan":[lines]}` and the human render is the same lines verbatim,
  so operator dry-run output stayed byte-identical when the seam landed. Lines come
  from `OpsCommand::display()` (already credential-masked), so this type never
  re-derives argv or reads a raw secret. It is also **composed** rather than
  duplicated: the other outputs carry a `DryRun` variant that delegates to it.
- **Seven command outputs** (`cli/src/commands.rs`) — `KillOutput`, `ResumeOutput`,
  `BudgetOutput`, `DeleteOutput`, `VersionsOutput`, `MemoryOutput`, `ApprovalsOutput`.
- **`ObservabilityOutput`** (`cli/src/observability.rs`) — the tier-aware
  observability surfaces (#460). Notable as the shape the seam is for: both the local
  and cluster tiers resolve their own `Endpoint` values and return *the same* output
  type, so tier parity is structural rather than two hand-aligned printers. That
  module is a deliberate leaf and never bypasses `CliOutput`.

## Known leakage

- **The trait is not the whole `--json` surface.** `CliOutput` governs the
  success path of the verbs that were converted. It cannot, by construction, prove
  that *every* verb returns an output rather than printing directly — that is a
  convention plus review, not a compile-time guarantee. A new handler can still call
  a stdout emitter and bypass the seam; nothing fails the build if it does.
- **No machine-readable schema.** Each `to_json` hand-builds its object with
  `serde_json::json!`. There is no committed JSON Schema and no drift gate, unlike
  the ACI (`packages/aci-protocol`, ADR-0017) or the channel protocol. An agent
  parsing this output is coupled to shapes that are only enforced by tests.
- **Not separately graded.** This is not one of the six swap-readiness Jobs in the
  vision doc: the "second implementation" here would be a second *output format*
  (YAML, a table protocol), which nobody has asked for. Per the governing restraint,
  the port is documented where the code already draws the line and no speculative
  formatter layer is added ahead of a real demand.

## Cross-links

- **Issue:** [#456](https://github.com/curie-eng/agentos/issues/456) — the `--json` contract broke per-command; `Ui::emit` + `CliOutput` + `DryRunPlan` are its fix
- **Issue:** [#460](https://github.com/curie-eng/agentos/issues/460) — the observability twin, whose local/cluster tiers share one `CliOutput`
- **Vision doc:** [architecture-vision.md](../../architecture-vision.md) — CLI output is not one of the six swap-readiness Jobs; not separately graded
- **ADR(s):** [ADR-0021](../../adr/0021-agentos-is-a-harness-for-coding-agents.md) — AgentOS is a harness for coding agents: the CLI's primary user is Claude Code (this seam is decision 1's enforcement); [ADR-0038](../../adr/0038-observability-cli-helper-for-the-agent-dev-loop.md) — the observability CLI is a thin client over the API proxy, not a second backend
