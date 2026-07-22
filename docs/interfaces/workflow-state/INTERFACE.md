---
seam: Workflow state store
kind: SOFT
impls: 1 (API state router)
grade: not separately graded
epics:
  - "#23"
  - "#248"
order: 14
---
# INTERFACE: Workflow state store

> Part of the AgentOS swappable-seam catalog — see the [seam index](../../interfaces.md).
<!-- BEGIN GENERATED: header (agentos dev docs-lint) -->
> **Kind:** SOFT &nbsp;·&nbsp; **Implementations today:** 1 (API state router) &nbsp;·&nbsp; **Swap-readiness grade:** not separately graded
<!-- END GENERATED: header -->

**Kind legend:** CLEAN = a real `Protocol`/typed port class · SOFT = swap via env/URL/prefix/wire, no code interface · NONE = not built yet.

## The black line

The durable workflow-state store is built. It shipped (#248, under epic #23) as the API
state router: a scoped KV/document store on Postgres JSONB exposing exactly the five verbs
this doc once said did not exist — get / put-with-CAS / list / delete / append. The swap
axis here is the state backend, and it is a SOFT seam: the store is reached over the HTTP
state API, not a typed in-process port, so a second backend is a persistence change behind
that API rather than a `Protocol` swap. A separate concrete route store, `AffinityStore`,
records one narrow thing (the `thread_key -> sandbox route` binding on Valkey, with atomic
acquire and TTL expiry) and is not the general store. The typed in-process port the kernel
would write arbitrary run state through (#23) is still unextracted, per "the second
implementation teaches the interface."

## Current contract

The state API is the store today. The five verbs live in
`apps/api/src/agentos_api/routers/state.py`:

- `get_state` (`apps/api/src/agentos_api/routers/state.py::get_state`)
- `put_state` — put with compare-and-swap (`apps/api/src/agentos_api/routers/state.py::put_state`)
- `list_state` (`apps/api/src/agentos_api/routers/state.py::list_state`)
- `delete_state` (`apps/api/src/agentos_api/routers/state.py::delete_state`)
- `append_state` (`apps/api/src/agentos_api/routers/state.py::append_state`)

Memory and Conversation history are the CLEAN loaders already built over this store
(`StateApiMemoryStore`, `StateApiTranscriptStore`).

Bundle code reaches the store two ways (#249), without shipping its own server. The
platform mounts an in-process `agentos-state` MCP server into every sandbox
(`runner/src/agentos_runner/state.py::build_state_server`), carrying get / set /
list / delete / append tools over the five router verbs; `memory` and `transcript`
are reserved so a skill cannot corrupt the memory or history namespaces. A bundle
script that talks to the store directly reads the same URL and scoped token from
`AGENTOS_STATE_URL` / `AGENTOS_STATE_TOKEN`. Both authenticate with the per-turn
scoped `state` token (ADR-0033), never the platform key.

The worker-side route store is separate. `AffinityStore` at
`apps/worker/src/agentos_worker/sandbox/affinity.py::AffinityStore` records the
`thread_key -> sandbox route` binding, and its methods are the closest thing to a
route-state contract:

- `get(thread_key) -> RouteRecord | None` (`apps/worker/src/agentos_worker/sandbox/affinity.py::AffinityStore.get`)
- `put_if_absent(thread_key, record, ttl_seconds) -> bool` — atomic acquire, the CAS-shaped primitive (`apps/worker/src/agentos_worker/sandbox/affinity.py::AffinityStore.put_if_absent`, `SET ... nx=True`)
- `replace(thread_key, record, ttl_seconds) -> None` (`apps/worker/src/agentos_worker/sandbox/affinity.py::AffinityStore.replace`)
- `touch(thread_key, ttl_seconds) -> bool` (`apps/worker/src/agentos_worker/sandbox/affinity.py::AffinityStore.touch`)
- `delete_if_claim(thread_key, claim_name) -> bool` — guarded delete via a Lua script (`apps/worker/src/agentos_worker/sandbox/affinity.py::AffinityStore.delete_if_claim`, script at `apps/worker/src/agentos_worker/sandbox/affinity.py::_DELETE_IF_CLAIM`)
- `live_claim_names(...) -> set[str]` (`apps/worker/src/agentos_worker/sandbox/affinity.py::AffinityStore.live_claim_names`)
- `mark_suspended(thread_key, history_ref, ttl_seconds) -> RouteRecord` (`apps/worker/src/agentos_worker/sandbox/affinity.py::AffinityStore.mark_suspended`)

The stored value is a `RouteRecord` (`apps/worker/src/agentos_worker/sandbox/types.py::RouteRecord`) JSON-serialized. This is route affinity, not general workflow state.

## Implementations today

One general store (the API state router over Postgres JSONB) plus one narrow concrete
route store: `AffinityStore` (`apps/worker/src/agentos_worker/sandbox/affinity.py::AffinityStore`), bound directly to `redis.Redis` and to the sandbox-routing use case. Neither is abstracted behind a typed workflow-state port yet.

## Known leakage

The placement constraint the future in-process port must honor is already visible in two
shapes. First, it is stateless-first: per ADR-0003 a suspend/resume is a cold pod restart
(the live process never survives), so resume rehydrates from a caller-supplied `history_ref`
injected as `AGENTOS_HISTORY_REF` rather than assuming any in-process or cache warmth
(`apps/worker/src/agentos_worker/sandbox/substrate.py::SandboxSubstrate.resume`). Second,
the route store leans on Valkey TTL-expiry as garbage collection: an idle route record
simply expires, and the reaper protocol depends on that automatic expiry. A durable
(non-TTL) backend for a future workflow-state port would have to add its own sweeper to
reclaim abandoned state, because it cannot inherit Valkey's expiry-as-GC for free.

## Cross-links

- **Epic(s):** #23 — full workflow state store API spec (get / put-CAS / list / delete / append); the store shipped as the API state router via #248, and the extracted in-process port lands under this epic
- **Vision doc:** [architecture-vision.md](../../architecture-vision.md) — not one of the six graded jobs
- **ADR(s):** [ADR-0003](../../adr/0003-stateless-first-rehydrate-on-resume.md) — stateless-first sessions; rehydrate on resume; no cross-hibernation cache assumption
