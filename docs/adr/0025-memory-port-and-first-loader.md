# 25. The memory port and its first loader: a scoped namespace over the durable state store

Date: 2026-07-13

Status: Accepted

Implements the memory seam for epic [#28](https://github.com/curie-eng/curie/issues/28),
issue [#264](https://github.com/curie-eng/curie/issues/264). This is the first
loader for `CURIE_MEMORY_REF`, which until now was a `SessionConfig` field
carried end-to-end but never dereferenced
([`docs/interfaces/memory/INTERFACE.md`](../interfaces/memory/INTERFACE.md)).

## Context

Agent memory (durable lessons an agent carries across sessions, with provenance
back to the turns they were learned from) is the seventh swappable job around the
opinionated core. Per ADR-0016, we do not build an adapter framework ahead of a
second implementation; we build the *first* implementation and let it define the
port. There was no first loader, so the port was unbuilt on purpose.

Two constraints shape the design:

- **Memory lives outside the sandbox** (ADR-0003, stateless-first). Sandboxes do
  not survive suspend/resume, so a resumed thread must rehydrate from an
  external, durable resource. An emptyDir-scratch or in-process cache would be
  lost on every suspend. So memory must be a network-reachable, rehydratable
  store resolved from `memory_ref` at boot.
- **We already landed a durable KV/document store** for #23/#248: an
  agent-scoped, namespaced, Postgres-JSONB-backed store with a log-shaped
  `append` endpoint and per-value/per-namespace size caps
  (`apps/api` `/agents/{agent_id}/state/{namespace}/{key}`). Standing up a
  second datastore (S3 objects, a bespoke memory service) for memory would be a
  new seam to operate for no benefit the state store does not already give.

## Decision

**Memory is a scoped namespace (`memory`) over the existing durable state
store, behind a small `MemoryStore` port.**

- **The port** (`runner/src/curie_runner/memory.py`) is a `Protocol` with two
  methods: `load() -> list[MemoryRecord]` and `append(record)`. No query
  language and no `consolidate` — consolidation is later work (#265/#266/#267).
  A `MemoryRecord` is `content` plus a `Provenance` (`learned_from_session_id`,
  `source_trace_ids`, `recorded_at`) — the entry→source-traces link the epic
  calls for.
- **The default backing** is `StateApiMemoryStore`: `load` GETs the single
  log-shaped key in the agent's `memory` namespace; `append` POSTs to that key's
  `/append` endpoint (#248). Durability, size caps, and survive-suspend/resume
  come from the state store for free. `NullMemoryStore` is used when no ref is
  configured, so the boot path is uniform.
- **`CURIE_MEMORY_REF` resolution:** the ref is the URL of the agent's memory
  namespace on the state API (`http(s)://api/agents/<id>/state/memory`). An
  `s3://` or other scheme is reserved for a future loader and rejected loudly at
  boot. The frozen ACI `SessionConfig.memory_ref` field is unchanged — no
  frozen-contract change. The state-API bearer is a **runner-local** knob
  (`CURIE_MEMORY_TOKEN`), like `CURIE_RUNNER_TOKEN`/`CURIE_MODEL`, not part
  of the frozen env.
- **Delivery into the sandbox:** at runner boot the store is resolved, prior
  memory is loaded, and it is composed into the effective system prompt as a
  memory preamble (so the model sees prior lessons as durable context). The write
  side is `SessionRunner.remember(...)`, which stamps provenance and appends.
- **Boot resilience:** an unsupported ref scheme fails the process loudly; a
  *transient* load failure degrades to "no memory" rather than blocking boot, so
  an agent still runs when its memory store is briefly unavailable.
- **The worker delivers the ref:** `binding.boot_env` sets `CURIE_MEMORY_REF`
  to the agent's namespace URL and forwards the API key as `CURIE_MEMORY_TOKEN`.

## Alternatives considered

- **A new S3/object-store memory backend.** Rejected: a second datastore to
  operate when the durable state store already gives durability, agent-scoping,
  namespacing, size caps, and an append log. The state store is the adopted
  spine for exactly this cross-turn durable state.
- **Auto-mounted memory MCP server for bundle code.** Deferred: exposing the
  store to the model as a tool (rather than a boot-time preamble + runner-driven
  append) is the same "later slice" the state store's own docstring flags. The
  port here does not preclude it.
- **Deriving `memory_ref` as an SDK resume id.** Rejected earlier and preserved:
  `history_ref` (SDK resume) and `memory_ref` (externalized memory) are distinct
  concepts (see `runner/config.py`); memory is not an SDK-resumable transcript.

## Consequences

- Memory is durable, provenance-carrying, and survives suspend/resume with no new
  infrastructure.
- **Known limitation:** the state API has one shared API key today, so forwarding
  it as the memory token grants the sandbox that key's full scope. A scoped,
  least-privilege memory token (or an API-side per-agent capability) is follow-up
  work, tracked with the same auth-hardening as the rest of the API.
- The automatic learned-record *extraction* that decides what to remember is out
  of scope here (#265/#266/#267); this ADR lands the port, the loader, boot
  wiring, and the write API `remember` calls.
