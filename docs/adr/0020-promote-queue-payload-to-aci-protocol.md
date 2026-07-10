# 20. Promote the queue payload into aci-protocol; keep it unversioned

Date: 2026-07-10

Status: Accepted

Records the decision to move the dispatcher→worker queue job into the frozen
tri-language contract and the version/strictness policy that promotion adopts.
Motivated by [#7](https://github.com/curie-eng/agentos/issues/7); extends
ADR-0012 (substrate- and channel-agnostic core) and follows the codegen
mechanism of ADR-0017.

## Context

`QueuedSlackEvent` — the normalized event the dispatcher enqueues on the Valkey
stream and the worker consumes — was **hand-mirrored in three places**: the
Python producer (`apps/dispatcher/.../queue.py`, the owner), a Rust copy
(`cli/src/queue.rs`), and imported by the worker. Only a golden fixture pinned
the wire bytes across languages. Hand-mirroring across languages is exactly the
silent-drift failure the frozen contracts exist to prevent (ADR-0017), so #7
calls for promoting the payload into `packages/aci-protocol` and, in the same
spirit, giving it channel-neutral field names.

This is being delivered in stages. This ADR + the accompanying PR are **Stage A
(PR-A)**: promote the model into `aci-protocol` with its field names *unchanged*,
so the change is wire-invisible (the on-stream JSON bytes are byte-for-byte
identical, proven by the golden fixture). The channel-neutral rename is a
separate follow-up (PR-B) because it touches the sacred kernel and is a
wire-format change requiring backward-compat aliases and a staged deploy.

## Decision

1. **Promote `QueuedSlackEvent` into `packages/aci-protocol`** as the single
   source of truth, generated into Rust and TypeScript like every other contract
   type. The CLI drops its hand-mirror and uses the generated crate
   (cli/CLAUDE.md: never hand-write the ACI types). The dispatcher subclasses the
   promoted model to add the Valkey stream transport helpers
   (`to_stream_fields`/`from_stream_fields`), which are producer/consumer
   plumbing, not part of the cross-language data shape — so the worker's import
   sites (the sacred `consumer.py`/`kernel.py`) are untouched.

2. **Do NOT bump `PROTOCOL_VERSION`; the queue struct is unversioned.**
   `PROTOCOL_VERSION` (`version.py`) versions the runner↔worker **NDJSON frame
   wire**, and the decoder rejects any frame whose `version` differs exactly. The
   queue payload is a *different wire* (a Valkey stream JSON blob) that carries no
   `version` field. Bumping `PROTOCOL_VERSION` for a queue-payload change would do
   nothing for the queue but would reject in-flight runner frames during a
   rollout — a nasty coupling through the shared constant. So the promoted queue
   type carries no on-wire version and is not added to the version-gated event
   unions. This is an explicit, documented exception to packages/CLAUDE.md's
   "bump on any model change" rule, which is aimed at the versioned NDJSON frames.

3. **The promoted model is strict (`extra="forbid"`)**, adopting the
   aci-protocol package convention (and matching the generated Rust
   `deny_unknown_fields`), rather than inheriting the dispatcher model's
   incidental leniency. This is wire-invisible for PR-A (the payload still carries
   exactly the seven fields); when PR-B renames fields, the rollout carries
   backward-compat aliases so a mixed-version fleet keeps parsing.

## Alternatives considered

- **Keep the model dispatcher-owned and hand-mirrored.** Rejected: it is the
  cross-language drift risk the freeze exists to prevent, and #7 explicitly wants
  it promoted.
- **Do the promotion and the channel-neutral rename in one change** (as #7's
  prose suggests). Rejected for staging: it would combine a frozen-contract PR, a
  sacred-kernel PR, and a wire-breaking rename in one diff with three review
  disciplines and blast radii, and would conflate a wire-invisible refactor with
  a wire-format change (impossible to roll back independently).
- **Bump `PROTOCOL_VERSION` for the promotion** (the literal packages/CLAUDE.md
  rule). Rejected: see decision 2 — it invalidates unrelated in-flight ACI runner
  frames.
- **Keep the promoted model lenient** to match the dispatcher's original. Rejected:
  packages/CLAUDE.md mandates strictness for aci-protocol and treats quiet
  relaxation as a version-policy change; strictness is wire-invisible here anyway.

## Consequences

- Two of the three hand-mirrors collapse into the generated contract; the golden
  fixture stays as the cross-language wire pin.
- The queue payload now lives in a package whose other members are all
  `PROTOCOL_VERSION`-gated NDJSON frames; the unversioned exception must be kept
  in mind if the queue payload ever needs its own versioning (give it an
  independent constant, never `PROTOCOL_VERSION`).
- **PR-B (follow-up):** channel-neutral field names (`conversation_id` keeps the
  thread-ts value; `reply_handle` becomes a nested `{channel, message_ts}` so
  binding, `chat.update`, and `setStatus` recover their Slack coordinates), with
  Pydantic/serde aliases + a flat→nested lift for a worker-before-dispatcher
  rolling deploy, landed as its own sacred-kernel-reviewed change.
