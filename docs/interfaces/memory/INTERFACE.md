# INTERFACE: Memory

> Part of the AgentOS swappable-seam catalog — see the [seam index](../../interfaces.md).
> **Kind:** CLEAN &nbsp;·&nbsp; **Implementations today:** 1 loader (`StateApiMemoryStore`) &nbsp;·&nbsp; **Swap-readiness grade:** not separately graded

**Kind legend:** CLEAN = a real `Protocol`/typed port class · SOFT = swap via env/URL/prefix/wire, no code interface · NONE = not built yet.

## The black line

The port is the `MemoryStore` `Protocol` in
`runner/src/agentos_runner/memory.py` (issue #264, ADR-0025). Two methods:

```python
class MemoryStore(Protocol):
    async def load(self) -> list[MemoryRecord]: ...
    async def append(self, record: MemoryRecord) -> None: ...
```

A `MemoryRecord` is `content: str` plus a `Provenance`
(`learned_from_session_id`, `source_trace_ids`, `recorded_at`) — the
entry→source-traces link. `SessionConfig.memory_ref`
(`packages/aci-protocol/src/aci_protocol/session.py:68`, `AGENTOS_MEMORY_REF`) is
resolved to a concrete `MemoryStore` at runner boot by `resolve_memory`. The
frozen ACI field is unchanged; the state-API bearer is a runner-local knob
(`AGENTOS_MEMORY_TOKEN`), not part of the frozen env.

## Current contract

- **Resolution.** `resolve_memory(memory_ref, env)`: an absent ref →
  `NullMemoryStore`; an `http(s)://` ref → `StateApiMemoryStore`; any other
  scheme (`s3://` …) is reserved for a future loader and rejected loudly.
- **Load side.** `load()` returns prior records oldest-first (empty when none).
  At boot the runner loads memory and composes it into the effective system
  prompt as a preamble — this is how memory is *delivered into the sandbox*. A
  transient load failure degrades to "no memory" and does not block boot.
- **Append side.** `append(record)` durably writes one record; provenance is
  stamped by `SessionRunner.remember(content, source_trace_ids=...)`. The record
  survives suspend/resume and is reloaded at the next boot.

## Implementations today

One: **`StateApiMemoryStore`**, backing memory as a scoped `memory` namespace
over the durable KV/document store landed for #23/#248
(`apps/api` `/agents/{agent_id}/state/{namespace}/{key}`, Postgres JSONB).
`load` GETs the single log-shaped key; `append` POSTs to that key's `/append`
endpoint (#248), inheriting durability and the per-value/per-namespace size caps.
The worker (`binding.boot_env`) delivers the ref as
`http(s)://api/agents/<id>/state/memory` and forwards the API key as the memory
token. `NullMemoryStore` is the no-ref sink.

## Known leakage

- **Shared API key as the memory token.** The state API has one shared API key
  today, so forwarding it into the sandbox as `AGENTOS_MEMORY_TOKEN` grants that
  key's full scope. A scoped, least-privilege memory token is follow-up work
  (ADR-0025, consequences).
- **No consolidate / no query.** The port is deliberately `load`/`append` only;
  consolidation and automatic learned-record extraction are later slices
  (#265/#266/#267). The load-bearing constraint remains: **memory lives OUTSIDE
  the sandbox** (ADR-0003) — the store is network-reachable and rehydratable, not
  pod-local state.

## Cross-links

- **Epic(s):** [#28](https://github.com/curie-eng/agentos/issues/28) — the memory port, `AGENTOS_MEMORY_REF` resolution, provenance record shape
- **Issue:** [#264](https://github.com/curie-eng/agentos/issues/264) — this first loader
- **Vision doc:** [architecture-vision.md](../../architecture-vision.md) — memory is not one of the six swap-readiness Jobs; not separately graded
- **ADR(s):** [ADR-0025](../../adr/0025-memory-port-and-first-loader.md) — the port + first loader; [ADR-0003](../../adr/0003-stateless-first-rehydrate-on-resume.md) — stateless-first; rehydrate on resume; externalize session state
