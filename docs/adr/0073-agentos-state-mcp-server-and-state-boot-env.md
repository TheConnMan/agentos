# 73. The durable state store reaches bundle code as an auto-mounted MCP server, not a bundle-shipped one

Date: 2026-07-22

Status: Accepted

Implements [#249](https://github.com/curie-eng/agentos/issues/249) (part of the
[#23](https://github.com/curie-eng/agentos/issues/23) durable workflow-state store epic).

## Context

The durable workflow-state store shipped under #248: the API state router over
Postgres JSONB (`/agents/{id}/state/{namespace}/{key}`), with get / put-CAS /
list / delete / append. Memory (#264) and conversation history (#20) already
consume it as two fixed namespaces, each through a runner-local loader that
dereferences an `AGENTOS_*_REF` URL with a scoped `state` token (ADR-0033).

What was still missing (#249) is a way for a *bundle skill* to read and write
general workflow state across a suspend/resume cycle. Without a platform-provided
path, every bundle that wanted durable state would ship its own MCP server and
either its own datastore or its own hand-rolled client against the state API,
duplicating the auth handling and re-solving the stateless-first rehydrate
problem ADR-0003 already answers.

## Decision

Expose the existing store to bundle code two ways, adding no new datastore and no
new credential.

**An auto-mounted `agentos-state` MCP server.** The runner wires an in-process
SDK MCP server into every real session, exactly as it already does for the
approval-request server (ADR-0010): `build_state_server`
(`runner/src/agentos_runner/state.py`) carries get / set / list / delete / append
tools that map one-to-one onto the router verbs. A skill calls
`mcp__agentos-state__*`; the backing is Postgres JSONB outside the sandbox, so the
data survives the cold-pod suspend/resume of ADR-0003 for free. `memory` and
`transcript` are reserved namespaces the tools refuse, so a skill cannot corrupt
the agent's learned lessons or its own transcript through the general door.

**An `AGENTOS_STATE_URL` / `AGENTOS_STATE_TOKEN` boot-env pair** for a bundle
script that talks to the store directly (a shell or python step, not the model).
Both are declared `BootEnv` fields (#488, ADR-0049), produced by the worker
binding per claim under the same closed-world contract that governs every other
`AGENTOS_*` var, so the name is typed once and a rename cannot silently drop the
feature. `AGENTOS_STATE_URL` is the agent's state namespace base
(`.../agents/<id>/state`); a `<namespace>/<key>` is composed onto it.

**One credential, reused.** `AGENTOS_STATE_TOKEN` is the same per-turn scoped
`state` token (ADR-0033) already minted for memory and history, presented as the
`X-API-Key` header and never logged. It is a signed derivative of `api_key`
bound to this agent and scope, never the platform key, so a sandbox reaching the
store still cannot resolve approvals or touch another agent's namespace. On the
no-api-key fake/local path no token is minted; the URL is still emitted (it is
not a credential), so the store is simply reached unauthenticated there.

**A patch protocol bump.** Two new optional `BootEnv` fields are a
backward-compatible addition, so `PROTOCOL_VERSION` moves 0.2.3 -> 0.2.4 (a patch
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
- `memory` and `transcript` are now reserved names in the general state door. A
  future fixed namespace (a third port over the store) must be added to that
  reserved set or a skill could clobber it.
