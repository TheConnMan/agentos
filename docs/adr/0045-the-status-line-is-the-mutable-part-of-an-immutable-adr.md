# 45. The status line is the mutable part of an immutable ADR

Date: 2026-07-16

Status: Accepted

**Amends ADR-0001** ("Record architecture decisions"). ADRs are immutable once
Accepted, so this is a new ADR rather than an edit to 0001. It **supersedes in
part** exactly one clause of 0001's Decision: "An ADR is immutable once Accepted;
to change a decision, add a new ADR that supersedes it and mark the old one
Superseded." Everything else in 0001 stands unchanged: one file per decision,
sequential numbering in `docs/adr/`, Nygard format, context/decision/consequences,
the prototype evidence, and the rule that a decision changes by a *new* ADR and
never by rewriting an old one's reasoning.

## Context

ADR-0001 states the immutability rule and its remedy in one sentence: an Accepted
ADR is immutable, and to change a decision you "add a new ADR that supersedes it
**and mark the old one Superseded**."

Those two halves contradict each other. Marking the old one Superseded *is* an
edit to an immutable file. The rule forbids its own remedy, and the remedy is the
only mechanism 0001 offers for recording that intent has moved.

The corpus shows exactly what that costs. **No ADR in this repo carries
`Status: Superseded` — not one of 43.** Supersessions did happen; they simply
went unrecorded on the superseded side:

- ADR-0039 opens with "**Amends ADR-0013** ... this is a new ADR that
  **supersedes in part** exactly one clause of 0013's Decision". ADR-0013
  contains no reference to 0039.
- ADR-0036 states "This ADR **amends the frozen-ACI posture of ADR-0005**".
  ADR-0005 contains no reference to 0036.
- ADR-0013's Alternatives record "The as-built queue is Valkey Streams via
  `redis-py`, not BullMQ", overtaking ADR-0007's Decision, which adopts "BullMQ +
  Valkey (queue)". ADR-0007 contains no reference to 0013.
- ADR-0026 and ADR-0027 each extract a Protocol ahead of a second adapter,
  narrowing ADR-0016's "we do not write adapter frameworks ... ahead of a real
  second implementation". Both disclaim superseding ADR-0007; neither names
  ADR-0016, and ADR-0016 contains no reference to either.

Every superseding ADR wrote its forward link. Not one superseded ADR carries the
back-link, because writing it was against the rules. The result is a corpus that
is honest read forward and misleading read backward: a reader who opens ADR-0007
finds a live-looking decision to adopt a queue library that appears nowhere in
the tree, with nothing on the page to warn them.

The forward-only link is not a substitute. Finding that 0007's queue clause is
dead requires reading all 43 ADRs looking for one that mentions it. The reader
who most needs the warning is the one who opened 0007 precisely because they did
not already know the corpus.

## Decision

**The `Status:` line and a superseded-by back-link are the mutable part of an
otherwise immutable ADR.** Specifically, an Accepted ADR may be edited in exactly
two ways, and no others:

1. **Its `Status:` line may change** — to `Superseded`, or to correct a status
   that no longer matches reality (a `Proposed` ADR whose decision has since
   shipped becomes `Accepted`).
2. **A back-link may be added** immediately after the `Status:` line, naming the
   ADR that superseded it and what was superseded.
3. **A pointer to a renamed or renumbered ADR may be repaired in place** — the
   link target and the `ADR-NNNN` label that names it, and nothing else on the
   line. This is permitted *only* when the referent is the same document under a
   new name: the old target no longer resolves, the new one resolves to the file
   the sentence already meant. The surrounding prose may not change by one word.

Clause 3 is not an exception to immutability; it is what immutability requires. A
renumbered link is not a claim about the tree that time falsified — it is a pointer
to a document that still exists and still says what it said. Repairing it preserves
the sentence's original meaning; leaving it to dangle destroys it, and the reader
loses the reasoning the ADR was written to keep. The test is mechanical and the
scope is one line: if repairing the pointer changes what the sentence asserts, it is
not a pointer repair and clause 3 does not reach it.

Everything else stays immutable. The Context, the Decision, the Alternatives, the
Consequences, the evidence, and the citations are the record of what was decided
and why **at that time**, and they are never rewritten — not to fix a stale symbol
name, not to soften a claim the tree has since disproved, not to make a shipped
system look better-planned than it was. A stale symbol name is a claim that the
tree has since moved past, and it stays: that gap between what was decided and what
was built is the record's whole point. An ADR whose reasoning was overtaken says so
via the back-link and leaves the original reasoning legible.

**Partial supersession keeps its status.** An ADR whose Decision was overtaken in
one clause but stands in the rest stays `Accepted` and carries a back-link
naming the superseding ADR and the clause. `Status: Superseded` is reserved for an
ADR whose Decision no longer holds *in whole*. This matches what 0039 and 0036
already do in prose from the forward side; it gives the backward side the same
vocabulary.

**A status is a claim about the tree, not about the release.** A `Proposed` ADR is
promoted to `Accepted` only on evidence that the decision is built — a symbol in
the tree, not a sentence in another ADR. A decision that has not shipped stays
`Proposed` however old or well-argued it is. Downgrading is not in scope here: an
`Accepted` ADR that was overtaken is recorded by back-link, never by quietly
reverting it to `Proposed`.

## Consequences

- The back-link is the only new prose an immutable ADR may gain. It is a pointer,
  not an argument: the reasoning for the change lives in the superseding ADR,
  where it is subject to the normal ADR discipline.
- `docs/adr/README.md` reads `Status:` verbatim from each file
  ([ADR-0043](0043-generated-interface-catalog-and-doc-lint-gate.md)), so a status
  correction is a one-line edit plus a regenerated index. The index is the surface
  where "which decisions are actually live" is answerable at a glance, which is
  only true if statuses are maintained.
- The four supersessions catalogued above become recordable, and are recorded as
  part of this decision's implementing work.
- The implementing work renumbered three ADRs (0029, 0038, 0039 → 0042, 0043,
  0044) to resolve prefix collisions, which left prose links inside Accepted
  ADR-0022 pointing at a moved file. Clause 3 authorizes that repair, and it is
  the only body edit this decision's own implementing work performs.
- The immutability rule gets *stronger*, not weaker: with a legal way to mark a
  decision dead, "the file was wrong so I fixed it" loses its last excuse. Any
  edit to an Accepted ADR outside the status line, the back-link, and a
  same-referent pointer repair is a violation of ADR-0001 as amended here.

## Alternatives considered

- **Make ADR bodies mutable; keep them current.** Rejected, and it is the failure
  mode this repo is built to avoid. An ADR that is edited to match today's tree is
  a snapshot of the present, which `ARCHITECTURE.md` already provides. The value
  of the corpus is the *intent-gap record*: what was decided, when, on what
  evidence, and how the thinking moved. Rewriting the body destroys exactly the
  thing the format exists to preserve, and it destroys it silently.
- **Leave 0001 alone; record supersession only in the superseding ADR.** Rejected:
  this is the status quo, and it produced 43 ADRs with zero `Superseded` statuses
  and four unrecorded supersessions. The forward link is written by the author who
  already knows; the back-link serves the reader who does not.
- **A separate `SUPERSESSIONS.md` ledger, leaving every ADR byte-frozen.**
  Rejected: it preserves immutability by putting the warning somewhere the reader
  of ADR-0007 will not look, and adds a second file to drift out of sync with the
  first. The warning belongs on the page that needs it.
- **Delete or archive overtaken ADRs.** Rejected: it discards the history of how
  intent shifted, which is the whole point of keeping the chain.
- **Widen the carve-out to a "Superseded by / Amended by" footer anywhere in the
  file.** Rejected as scope: pinning the back-link directly under `Status:` keeps
  the mutable region contiguous, small, and reviewable — a diff touching an
  Accepted ADR outside those lines is mechanically suspect.
