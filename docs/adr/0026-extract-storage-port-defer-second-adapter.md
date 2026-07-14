# 26. Extract the storage port now; defer the second (GCS/Azure) adapter

Date: 2026-07-13

Status: Accepted

Refines how ADR-0007 (adopt-not-build) applies to the blob-storage seam. Issue
[#282](https://github.com/curie-eng/agentos/issues/282), epic #83. Does **not**
supersede ADR-0007 — it keeps 0007's core rule (no second backend ahead of
demand) and changes only *where the line is drawn in code* for this one seam.

## Context

ADR-0007 and the shipped blob-storage INTERFACE.md hold that "the S3 protocol IS
the port": there is no `StorageInterface` class, and one is extracted only when a
non-S3 backend demands it, because the second implementation teaches the
interface. That restraint has a real cost the INTERFACE already flags: the seam
bleeds through **three hand-aligned client sites** (the API's boto3 writer, the
worker's boto3 reader, the chart's `mc` init) that must agree on
endpoint/credentials/bucket/addressing by convention, not by any shared type. The
write-once/no-mutation immutability guarantee likewise lives only in prose.

Two things are true at once: (a) there is no non-S3 customer, so building a
GCS/Azure *adapter* would be speculative and is correctly deferred; but (b) the
five S3 operations and the write-once discipline are already stable and known —
they are not a guess about a future backend, they are today's contract. Naming
that contract in code is not the same speculative act 0007 warns against
(inventing an abstraction whose *shape* is a guess).

## Decision

Extract the port as a **structural `Protocol`, not a rewrite**, and defer the
adapter:

- `ObjectStore` `Protocol` in `apps/api/.../storage.py` captures the five ops
  (`ensure_bucket`/`exists`/`put`/`get`) and **promotes the write-once/no-mutation
  guarantee from convention into the port's documented contract** (the ticket's
  explicit ask). The existing `BundleStore` structurally satisfies it unchanged;
  consumers (`deps`/`gitflow`/`deploy`) now type against `ObjectStore`, so a
  second backend is a drop-in that implements the Protocol rather than a rewrite
  of every call site.
- A shared `build_s3_client(settings)` factory centralizes the path-style client
  construction, removing one copy of the "hand-aligned" duplication.
- The worker keeps its own `BundleReader` Protocol (its read-only slice of the
  port), because it deliberately does not import the API package (`binding.py`).
- **No second adapter is built.** GCS/Azure remains gated on a genuine non-S3
  customer, exactly as ADR-0007 and #282 require. The chart's `mc` init is left
  as-is (a third dialect) and noted as the remaining aligned site.

## Alternatives considered

- **Do nothing (leave the seam un-abstracted, per the letter of 0007).** Rejected
  for this slice: the five ops and write-once rule are stable *today*, so naming
  them is documentation-in-code, not speculation; and it makes the eventual
  adapter a genuinely small, reviewable change instead of a three-site sweep.
- **Build the GCS/Azure adapter now.** Rejected: no non-S3 demand, nothing to
  verify against — this is the speculative build 0007 exists to prevent.
- **A new shared `storage` package both apps import.** Rejected as churn for one
  impl; the worker's no-API-import boundary makes a local read Protocol the
  lighter, honest seam. Consolidation can come with the second backend.

## Consequences

- The port is real and typed; the write-once guarantee is contractual, not just
  convention. A future GCS/Azure adapter implements `ObjectStore` /
  `BundleReader` and wires into the same consumers with no call-site churn.
- The chart `mc` init and the worker/API client construction are still two
  physically separate S3 clients; fully unifying them (or the `mc` path) is left
  for when the second backend lands. INTERFACE.md is updated to Kind CLEAN with
  this scope noted.
