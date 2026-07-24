# 43. The interface catalog is generated from declared front-matter and gated in CI

Date: 2026-07-16

Status: Accepted

Implements [#452](https://github.com/curie-eng/curie/issues/452).

## Context

The swappable-seam catalog in `docs/interfaces/` rotted. A review of all 17 seams
found it could not be trusted in either direction without re-reading the source,
which is the whole job it exists to do: `harness-modelsession/INTERFACE.md`
overstated cleanliness (claiming a refactor that had not merged), `channel-ingress`
and `queue-stream` understated it (describing a `QueuedSlackEvent` payload that #7
had already promoted to the channel-neutral `QueuedTurn`), and `docs/interfaces.md`
contradicted itself (listing Workflow state as `NONE | 0 impls` while adjacent rows
credited loaders built on that exact store).

The corrections matter, but they are not the durable point. The catalog rotted for
one structural reason: **the same fact is written in two places and checked in
zero.** The index (`docs/interfaces.md`) restates the Kind / Impls / Grade that each
seam doc already declares, and nothing compares them. Line-number citations
(`file.py:NN`) rot because they encode a coordinate that every insertion above them
invalidates. Both are the same defect, an unverified duplicate. Written once and
left alone, any hand-correction rots back to the same state within a fortnight.

The repo already answers this defect class for its typed contracts: one declared
source of truth, a generator, and a regenerate-and-diff check in CI
(`scripts/check-contracts.sh`, the `contracts-ts` job, the UI `gen:manifest` diff).
The ask is to apply that same pattern to the catalog.

## Decision

**Make the interface catalog machine-checkable by generating it from a single
declared source per seam and gating it in CI.** Three parts:

- **YAML front-matter on each `INTERFACE.md` is the single declared source.** Each
  seam doc carries a front-matter block declaring `seam`, `kind`, `impls`, `grade`,
  `epics`, `order`, and an optional `epic_note`. This is the one place the seam's
  catalog facts are stated. Front-matter is chosen because it is unambiguous to
  parse, invisible in the renderers GitHub uses, and separates the declared fact
  from the prose that explains it. The audit found the premise that these fields
  already existed to be false: sixteen docs carried the facts only in a prose
  blockquote, one carried a different blockquote shape entirely, and the index's
  Epic(s) column lived nowhere in the docs and silently disagreed with them. Writing
  the front-matter forces each of those contradictions to be resolved by a human,
  once. That reconciliation is the ticket's real value.

- **A generator emits the derived views from that source.** `curie dev docs-lint`
  regenerates the seam table in `docs/interfaces.md` and each doc's header
  blockquote from the front-matter, replacing only the region between explicit
  `<!-- BEGIN/END GENERATED -->` markers so hand-written prose and generated content
  coexist in one file. The blockquote becomes generated output, not hand prose, so
  the index can no longer disagree with the docs it summarizes.

- **A CI doc-lint gates the catalog.** The same `curie dev docs-lint` command
  (wrapped by `scripts/check-docs.sh`, mirroring `check-contracts.sh`: regenerate,
  then `git diff --exit-code`) runs in the existing `python` job and fails on: any
  line-number citation under the linted root (every recognized extension, and the
  GitHub `#L` spelling); any cited file path that does not exist; any cited Python
  symbol that does not resolve (by a static `ast` parse, never an import); and an
  index table that does not match what the seam docs' front-matter declares.
  Citations become symbol-form (`authorizer.py::authorize_approval`), which survives
  refactors where a line number rots on the next insertion above it.

The tool is Python in a `tools/doclint` uv workspace member, behind a thin
`curie dev docs-lint` clap wrapper that shells the script exactly like its
`dev contracts` / `dev chart-check` siblings.

## Alternatives considered

- **Regex over the existing prose blockquote** (no front-matter). Rejected: it
  encodes the current prose wording as a grammar and breaks on the next reword,
  which is the exact rot this ADR exists to stop. It also has no source at all for
  three of the index's columns, which are not in the blockquote.
- **A Rust implementation under `curie dev`.** Rejected: the resolver must parse
  Python to resolve cited symbols, and Python's `ast` module is the correct, free
  tool. A Rust reimplementation of Python symbol resolution is a real bug surface
  for zero user-facing benefit, and the `dev` namespace's own siblings are thin
  shell-outs to repo scripts, not Rust implementations.
- **A bare `scripts/*.sh` with no CLI surface.** Rejected: root `CLAUDE.md` is
  explicit that a loose script should be the exception with a reason. The discoverable
  `curie dev` surface is the convention; the script stays the implementation.
- **Generating the Swap-readiness grade table too** (the `docs/interfaces.md` <->
  `docs/architecture-vision.md` verbatim duplicate). Deferred, not adopted this run:
  the grade table's source is a prose narrative with no per-seam home, so inventing
  one is a second architecture change riding a docs PR. Named as a follow-up.

## Consequences

- The index cannot silently disagree with the seam docs: it is regenerated from
  their declared front-matter and CI diffs the result. A cited path that is deleted
  or a symbol that is renamed fails CI on the renaming PR, dragging the stale prose
  in front of a human instead of letting it rot silently. This is the gate working,
  and the first few unrelated PRs after it lands should expect to hit it.
- **Accepted limit: the gate coordinate-checks and index-consistency-checks; it does
  NOT verify prose truth.** A doc can cite a real path and a resolving symbol while
  the sentence around them is false, and every seam correction in #452 is exactly
  that class of defect, meaning the gate this ADR builds would not have caught the
  bugs it is fixing. Checking prose against code is a reviewer, not a lint, and
  pretending otherwise is how the catalog earned its reputation. The gate's claim is
  narrow and exact: it makes a false claim harder to write accidentally and
  impossible to keep silently. It converts silent rot into a build failure; it does
  not convert prose into proof. A green `docs-lint` badge means the catalog is
  coordinate-checked and index-consistent, not that its prose is verified.
- **`docs/adr/` is excluded from the linted root.** ADRs are immutable once
  Accepted (root `CLAUDE.md`); an ADR's line citation is a record of what its author
  was looking at on that date, not a claim about today's code, and rewriting it to
  satisfy a lint would corrupt the historical record. The exclusion is a constant in
  the tool, covered by a test, so the decision is visible in code rather than in a
  forgotten conversation. This means the issue's "no line-number citations remain
  under `docs/`" is met for every path except `docs/adr/`, a deliberate and
  documented deviation.
- Symbol resolution is Python-only this run; a `::symbol` on a non-Python path is a
  hard error (not a silent pass), and Rust/TS resolvers are named follow-up work.
- The catalog now has a maintenance contract: a new seam is picked up by globbing
  `docs/interfaces/*/INTERFACE.md`, so a seam added without front-matter fails the
  gate rather than entering the catalog ungated.
