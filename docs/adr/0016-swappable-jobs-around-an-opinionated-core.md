# 16. Swappable jobs around an opinionated core; the second implementation teaches the interface

Date: 2026-07-09
Status: Accepted

**Superseded in part by [ADR-0026](0026-extract-storage-port-defer-second-adapter.md)
and [ADR-0027](0027-thin-broker-port-defer-second-broker.md)**
(back-link added under [ADR-0045](0045-the-status-line-is-the-mutable-part-of-an-immutable-adr.md)):
the Decision below says "We do not write adapter frameworks (`StorageInterface`,
`ChannelAdapter`, a pluggable channel registry) ahead of a real second
implementation." 0026 extracted an `ObjectStore` Protocol and 0027 a
`StreamBroker` / `StreamPublisher` Protocol, each ahead of a second adapter and
each arguing why. Only that clause is narrowed — the governing rule below stands.

Retroactive record of the design discipline described as-built in
[`docs/architecture-vision.md`](../architecture-vision.md). This is the governing
principle for every future "should we abstract X?" question, so it needs to be a
decision on record, not just a doc.

## Context

Curie is an opinionated core (dispatcher, queue, worker kernel, sandbox
substrate, runner behind the frozen ACI, the API's git-flow engine, the UI)
surrounded by six jobs a production agent platform must do, each of which has one
or more plausible vendors: observability, evals, blob storage, a relational
store, a harness, a communication channel. The standing temptation on a system
like this is to build a clean adapter interface for each job up front. A small
team that does that spends its budget maintaining speculative abstraction layers
whose shape is a guess about a second implementation that does not exist yet.

## Decision

Hexagonal in spirit, with discipline: **one implementation per port today**, and
each port is defined by where the code already draws the line, not by a
speculative interface layer. We do not write adapter frameworks
(`StorageInterface`, `ChannelAdapter`, a pluggable channel registry) ahead of a
real second implementation. A port is promoted from convention to a frozen
contract only when a real swap demand arrives, the way the ACI protocol already
was. Until then the boundary is held by convention plus review: the boundary
files are known and small, cross-seam imports are flagged in review, and only the
two contracts that genuinely must not drift (`aci-protocol`, `plugin-format`) are
frozen in CI. The swap-readiness of each of the six jobs, and where each seam
honestly is not clean yet, is catalogued in
[`docs/architecture-vision.md`](../architecture-vision.md).

## Alternatives considered

- **Write the adapter interfaces now (a `StorageInterface`, a channel-adapter
  framework, a generic eval-store interface).** Rejected: with one implementation,
  the interface encodes guesses about the second implementation's needs and is
  wrong in the ways that matter. The second implementation is what teaches the
  interface; writing it first is negative work.
- **Hard-couple the core to today's vendors (Langfuse, Slack, MinIO, Postgres) and
  stop pretending they are swappable.** Rejected: it forecloses the leave-behind
  portability story (a customer's managed Postgres, their S3, an air-gapped model)
  that the product sells. The seams stay honest even with one adapter each.
- **A generic plugin/registry framework for all six jobs.** Rejected as premature
  abstraction: it is the commodity-orchestration trap ADR-0007 already declined,
  applied to the seams instead of the engine.

## Consequences

- This is the meta-rule ADR-0012 (substrate/channel seams) is an instance of, and
  the cousin of ADR-0007 (adopt-not-build): 0007 governs what we build versus
  adopt, this governs how we seam what we built and when to harden a seam.
- The open "is this seam a real swap axis" epics
  ([#83](https://github.com/curie-eng/curie/issues/83),
  [#84](https://github.com/curie-eng/curie/issues/84),
  [#85](https://github.com/curie-eng/curie/issues/85),
  [#86](https://github.com/curie-eng/curie/issues/86)) are answered by this
  discipline: a seam gets promoted to a contract when a user needs the swap, not
  before. Backfilling `INTERFACE.md` at each seam
  ([#53](https://github.com/curie-eng/curie/issues/53)) documents the black
  lines without prematurely abstracting them.
- A PR that adds a speculative abstraction layer ahead of a real second
  implementation is violating this ADR, even when the code is clean.
