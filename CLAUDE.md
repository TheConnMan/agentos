See [AGENTS.md](AGENTS.md) - the agent instructions for this repo live there.

## Architecture Decision Records (ADRs)

`docs/adr/` is the system of record for architecture decisions. Each ADR captures
**what was decided and when**, along with the reasoning and the alternatives
weighed at that time. That record is durable even after a decision evolves: ADRs
are immutable once Accepted, so when the thinking changes you add a **new ADR that
supersedes** the old one rather than editing it. The chain of supersessions is the
history of how intent shifted over time (the intent-gap record), not just a
snapshot of the current state.

Workflow for non-trivial work:

- **Write an ADR for the decision** before or alongside the work, numbered
  sequentially in `docs/adr/`.
- **Issues reference their ADR(s).** A GitHub issue that implements a decision
  links the ADR, so an agent picking up the issue reads the decision's intent
  first and builds to it.
- Division of labor: **ADRs are the "why + when"**, **issues are the "what + track
  the work"**, and **`ARCHITECTURE.md` is the "what talks to what."** Keep decision
  rationale in the ADR, not duplicated across issues.
