# INTERFACE: Workflow state store

> Part of the AgentOS swappable-seam catalog — see the [seam index](../../interfaces.md).
> **Kind:** NONE &nbsp;·&nbsp; **Implementations today:** 0 abstracted (a concrete `AffinityStore` exists) &nbsp;·&nbsp; **Swap-readiness grade:** not separately graded

**Kind legend:** CLEAN = a real `Protocol`/typed port class · SOFT = swap via env/URL/prefix/wire, no code interface · NONE = not built yet.

## The black line

There is no workflow-state port yet — only a concrete, single-purpose route store. Today's `AffinityStore` records exactly one thing: the `thread_key -> sandbox route` binding on Valkey, with atomic acquire and TTL expiry. A general workflow state store (durable get / compare-and-swap put / list / delete / append for agent run state, checkpoints, and history) is unbuilt. The intended line, when extracted, is a small typed port the kernel writes state through, with Valkey (or Postgres) behind it — but per "the second implementation teaches the interface," the port lands only when a real second consumer demands it.

## Current contract

No port class exists. The concrete `AffinityStore` at `apps/worker/src/agentos_worker/sandbox/affinity.py:31` is what exists today, and its methods are the closest thing to a state contract:

- `get(thread_key) -> RouteRecord | None` (`affinity.py:42`)
- `put_if_absent(thread_key, record, ttl_seconds) -> bool` — atomic acquire, the CAS-shaped primitive (`affinity.py:49`, `SET ... nx=True`)
- `replace(thread_key, record, ttl_seconds) -> None` (`affinity.py:59`)
- `touch(thread_key, ttl_seconds) -> bool` (`affinity.py:64`)
- `delete_if_claim(thread_key, claim_name) -> bool` — guarded delete via a Lua `_DELETE_IF_CLAIM` script (`affinity.py:69`, script at `affinity.py:18`)
- `live_claim_names(...) -> set[str]` (`affinity.py:74`)
- `mark_suspended(thread_key, history_ref, ttl_seconds) -> RouteRecord` (`affinity.py:96`)

The stored value is a `RouteRecord` (`sandbox/types.py:54`) JSON-serialized. This is route affinity, not general workflow state — the get/put-CAS/list/delete/append surface epic #23 specifies does not exist here.

## Implementations today

Zero abstracted implementations. One concrete class, `AffinityStore` (`affinity.py:31`), bound directly to `redis.Redis` and to the sandbox-routing use case.

## Known leakage

The port does not exist yet, so there is nothing to leak through — but the placement constraint the future store must honor is already visible: it is stateless-first. Per ADR-0003, a suspend/resume is a cold pod restart (the live process never survives), so resume rehydrates from a caller-supplied `history_ref` injected as `AGENTOS_HISTORY_REF` rather than assuming any in-process or cache warmth (`sandbox/substrate.py:99`–`139`). A future workflow-state port must not assume process affinity or cache continuity across a suspend; state that must survive has to be written durably, keyed by thread/session, and rehydratable from cold.

## Cross-links

- **Epic(s):** #23 — full workflow state store API spec (get / put-CAS / list / delete / append); the extracted port lands with this epic, and its interface AC is the gold standard for this seam
- **Vision doc:** [architecture-vision.md](../../architecture-vision.md) — not one of the six graded jobs
- **ADR(s):** [ADR-0003](../../adr/0003-stateless-first-rehydrate-on-resume.md) — stateless-first sessions; rehydrate on resume; no cross-hibernation cache assumption
