# 3. Stateless-first sessions; rehydrate on resume; no cross-hibernation cache assumption

Date: 2026-07-04
Status: Accepted

## Context

The interactive design wants warm, steerable sessions with prompt-cache reuse across turns. The open question was whether Agent Sandbox hibernation preserves a *live process* across suspend/resume — because if it does, a thread can hold an in-RAM session (and a warm prompt cache) indefinitely; if it does not, every resume is a cold start and the design must externalize state.

## Decision

Treat sessions as **stateless-first**. Session state (conversation history, memory pointer, plugin version) is externalized; a resumed thread **rehydrates from history**, it does not assume a surviving in-RAM process. Prompt-cache warmth is treated as an optimization that exists only *within a single continuous claim*, never across a suspend/resume. The cost/budget model assumes cache-cold on resume, and the hibernation TTL policy is therefore a direct cost lever.

## Evidence (live, scratch cluster, 2026-07-04)

- The `Sandbox` lifecycle control is `operatingMode: Running | Suspended` with `shutdownPolicy: Retain | Delete`. Setting `operatingMode: Suspended` **deleted the pod** (observed pod-exists 1→0 in ~6s); resume created a **new pod with a new UID and start time**. `serviceFQDN` re-bound to the fresh pod (routing identity is durable) but the process did not survive. With emptyDir scratch, in-pod state was lost; only a PVC + `Retain` would persist the *volume* (not the process).
- Cache warmth *within* a live claim is real and strong: a runner holding one SDK session inside a sandbox served two calls where call-2 `cache_read_input_tokens = 16045`, exactly the 16045 created on call 1. So the optimization is worth having — just not across a suspend.

## Consequences

- The worker's resume path must deterministically rehydrate the session and re-establish the `thread_ts → sandbox_id` route. This interacts with the finish-race in the concurrency kernel and is a primary place duplicate side effects can hide (see the de-risking review §8 risk 1).
- Never put a content-based model router in front of the harness — it breaks the prefix cache that makes even the within-claim optimization worthwhile.
- Pricing/budgets model the common case as cache-cold on resume.
