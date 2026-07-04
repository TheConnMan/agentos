# 1. Record architecture decisions

Date: 2026-07-04
Status: Accepted

## Context

AgentOS's architecture was shaped by a set of decisions that were each validated against a live prototype before being committed, rather than chosen on paper. Those decisions and their evidence need to survive past the people who ran the prototypes, so future contributors understand *why* the architecture is the way it is and do not re-litigate settled questions.

## Decision

We will keep Architecture Decision Records (ADRs) in `docs/adr/`, one file per decision, numbered sequentially. Each records the context, the decision, and the consequences — and, because this project is evidence-driven, the concrete prototype evidence behind the decision where one exists.

Format is lightweight (Michael Nygard style). An ADR is immutable once Accepted; to change a decision, add a new ADR that supersedes it and mark the old one Superseded.

## Consequences

- New contributors read `docs/adr/` to understand the shape of the system and the reasoning.
- Decisions carry their evidence, so "why not X" is answerable without re-running a prototype.
- The reference design (`docs/reference/detailed-architecture.md`) is the detailed spec; ADRs are the load-bearing choices distilled out of it plus what the prototypes proved.
