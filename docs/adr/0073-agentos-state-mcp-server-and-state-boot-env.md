# 73. The durable state store reaches bundle code as an auto-mounted MCP server, not a bundle-shipped one

Date: 2026-07-22

Status: Accepted

Implements [#249](https://github.com/curie-eng/curie/issues/249) (part of the
[#23](https://github.com/curie-eng/curie/issues/23) durable workflow-state store epic).

## Context

The durable workflow-state store shipped under #248: the API state router over
Postgres JSONB (`/agents/{id}/state/{namespace}/{key}`), with get / put-CAS /
list / delete / append. Memory (#264) and conversation history (#20) already
consume it as two fixed namespaces, each through a runner-local loader that
dereferences an `CURIE_*_REF` URL with a scoped `state` token (ADR-0033).

What was still missing (#249) is a way for a *bundle skill* to read and write
general workflow state across a suspend/resume cycle. Without a platform-provided
path, every bundle that wanted durable state would ship its own MCP server and
either its own datastore or its own hand-rolled client against the state API,
duplicating the auth handling and re-solving the stateless-first rehydrate
problem ADR-0003 already answers.

## Decision

Expose the existing store to bundle code two ways, adding no new datastore and no
new credential.

**An auto-mounted `curie-state` MCP server.** The runner wires an in-process
SDK MCP server into every real session, exactly as it already does for the
approval-request server (ADR-0010): `build_state_server`
(`runner/src/curie_runner/state.py`) carries get / set / list / delete / append
tools that map one-to-one onto the router verbs. A skill calls
`mcp__curie-state__*`; the backing is Postgres JSONB outside the sandbox, so the
data survives the cold-pod suspend/resume of ADR-0003 for free. `memory` and
`transcript` are reserved namespaces: the tools refuse them client-side for a
fast, legible error, and the API refuses them for the bundle's token server-side
(below) so the refusal cannot be bypassed by calling the store directly.

**An `CURIE_STATE_URL` / `CURIE_STATE_TOKEN` boot-env pair** for a bundle
script that talks to the store directly (a shell or python step, not the model).
Both are declared `BootEnv` fields (#488, ADR-0049), produced by the worker
binding per claim under the same closed-world contract that governs every other
`CURIE_*` var, so the name is typed once and a rename cannot silently drop the
feature. `CURIE_STATE_URL` is the agent's state namespace base
(`.../agents/<id>/state`); a `<namespace>/<key>` is composed onto it.

**Two scoped tokens, one reserved-namespace boundary.** The reserved namespaces
are load-bearing, so the guarantee must hold against a bundle that skips the tool
and calls `CURIE_STATE_URL` directly with the token it was handed. The memory
and history loaders and the bundle reach *different* namespaces, so they get
*different* tokens (both ADR-0033 signed derivatives of `api_key`, bound to this
agent, never the platform key, never logged, both `X-API-Key`):

- the memory/history loaders keep the broad `state` scope, which the router
  accepts on every namespace -- they MUST read and write `memory`/`transcript`
  to rehydrate the agent across suspend/resume;
- `CURIE_STATE_TOKEN` is a narrower `state.app` scope, which the router refuses
  (403) on the reserved namespaces (`forbid_reserved_namespace` in
  `apps/api/.../routers/state.py`). Because the bundle only ever holds the narrow
  token, the reserved-namespace rule is enforced where the authority actually
  lives -- the server -- not merely asked for by the client.

On the no-api-key fake/local path neither token is minted; the URL is still
emitted (it is not a credential), so the store is simply reached unauthenticated
there.

**A patch protocol bump.** Two new optional `BootEnv` fields are a
backward-compatible addition, so `PROTOCOL_VERSION` moves 0.2.4 -> 0.2.5 (a patch
under the 0.x rule; tolerant consumers ignore an unknown field) and the committed
schema, wire lock, and generated Rust/TypeScript are regenerated from the model.

## Consequences

- A bundle skill gets durable, suspend/resume-surviving state with zero
  bundle-shipped server and zero datastore of its own; the auth handling lives in
  one place and matches memory/history.
- The change touches the frozen wire contract (schema + `PROTOCOL_VERSION` +
  `wire.lock` + generated crates), so it must clear the contract gate; it is a
  compatible patch, but reviewers should treat the regenerated artifacts as
  load-bearing.
- The full suspend/resume survival is proven end to end only on a live cluster.
  The unit tests cover the store-backed read/write path (client and every tool op
  against an in-memory fake of the router) plus a fresh-client-reads-prior-write
  check that stands in for the persistence the survival depends on; the live
  cycle is left to a runtime/soak exercise.
- `memory` and `transcript` are now reserved names in the general state door,
  enforced server-side against the bundle's `state.app` token. A future fixed
  namespace (a third port over the store) must be added to the router's reserved
  set (`RESERVED_NAMESPACES`) AND the runner tool's client-side set, which are two
  byte-mirrored literals, or a skill could clobber it.
- **Residual, honestly scoped.** The broad `state` token backing the loaders is
  still an env var (`CURIE_MEMORY_TOKEN`/`CURIE_HISTORY_TOKEN`) inside the
  same sandbox as bundle code, and it cannot be scrubbed after boot because the
  ports write memory/transcript *during* the turn. So a bundle that deliberately
  scrapes another env var can still reach the reserved namespaces. This is
  defense against accidental corruption and the documented direct-URL path -- not
  a hard boundary against a hostile in-sandbox actor, which is out of the
  per-agent threat model (the bundle is the operator's own code, the token is
  per-agent, and the blast radius is the agent's own state). Closing that fully
  needs the port I/O moved out of the bundle's process, a larger change deferred
  to its own ADR.
