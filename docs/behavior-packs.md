# Behavior packs

Per-agent, opt-in UX touches applied around a turn, so an agent owner can enable
them for their deployments without imposing them on every other agent on the
install:

- **load** -- a sampled "working..." load line shown while a turn runs.
- **tips** -- a sampled capability tip ("I can rank leaks by $"). Separate from
  load: a load line is what the agent is doing now, a tip advertises what it can
  do, and an agent can enable either without the other.
- **greeting** -- a canned reply to a *bare* greeting ("hi", "hey there team")
  that never calls the model.
- **help** -- a canned reply to a *bare* help / "what can you do" request, also
  without a model call (the niceties battery's help half).
- **settings** -- a declarative allowlist of user-editable runtime knobs (the
  template's user-settings battery), with platform-owned validation. Schema only
  in this PR; the durable override store and edit UI are a deferred runtime.
- **nav** -- the no-dead-ends hub button: a way back to the agent's home/help
  screen, appended to a structured reply's buttons when none links there.

These are illustrative, not the point. The point is the mechanism: a
per-agent, opt-in, declarative config layer, resolved at bind time, that an owner
enables for their own deployments with no effect on any other agent. New pack
types slot into the same mechanism. The battery-by-battery mapping below shows
which of the template's all-agent features do and do not fit it, and why.

## Why packs are declarative data, not code

A pack is JSON on the agent's row: phrases, working lines, a reply string. The
sampler and greeting matcher (`agentos_worker.behaviorpacks`) are platform-owned
and identical for every agent; only the data varies. This is deliberate: pack
content never executes, so enabling a pack can never run an agent's code, and the
sandbox-isolation guarantee holds without any pack crossing into the runner. If a
future pack needs real logic, that logic must run inside the sandbox, not in the
worker.

## What is built (this PR)

The substrate, end to end, minus the kernel call sites:

- **Storage + API** (`apps/api`): a nullable `agents.behavior_packs` JSONB column
  (migration `0005`), validated by `schemas.BehaviorPacksConfig`, accepted on
  `POST /agents`, and read/written via `GET|PUT /agents/{id}/behavior-packs`
  (mirrors the budget control endpoints). NULL reads as all-off.
- **Logic** (`apps/worker/src/agentos_worker/behaviorpacks.py::sample_load`): `sample_load(packs, seed)`,
  `sample_tip(packs, seed)`, `match_greeting(packs, text)`,
  `match_help(packs, text)`, plus the settings pack's `coerce_setting(setting,
  raw)` and `resolve_settings(packs, overrides)`. Pure stdlib, fully unit-tested.
  `load` and `tips` sample independently off the same seed (distinct salts), each
  returning only its own content; the display surface composes them. The two
  matchers share one bare-utterance core: a reply only for a phrase said alone
  (or with trailing filler); a phrase glued to a real request ("hi show me the
  report") returns `None` and falls through to the model. `resolve_settings`
  layers a validated override over each declared default and ignores
  unknown/invalid keys, so a stale store can never break resolution.
- **Binding** (`apps/worker/src/agentos_worker/binding.py::BindingResolver`, not the sacred kernel): the resolver
  now selects `behavior_packs`, carries it on `ResolvedDeployment`, and exposes
  `BindingResolver.packs_for(resolved) -> BehaviorPacks`.

## What is NOT built: the deferred runtimes

Two pieces are intentionally out of this PR, each a natural follow-up on top of
the substrate here.

### The settings-pack override store + edit UI

The `settings` pack ships its schema and validation (`coerce_setting`,
`resolve_settings`). What it does not ship is the durable per-agent override
store the resolved values layer onto, and the surface (a Slack modal / an API
route) an owner uses to change them live. `resolve_settings(packs, overrides)`
is already the function that runtime will call; wiring a store into it is
additive and touches neither the kernel nor a frozen contract.

### The kernel wiring for tips/greeting/help (needs F1 review)

These touches fire from inside `apps/worker/src/agentos_worker/kernel.py::Kernel`, which is the F1 "sacred"
module: any change needs the escalated adversarial review (spec-vs-impl +
side-effects-detective) per `apps/worker/CLAUDE.md`. The greeting short-circuit
also brushes against kernel rule 3 ("the kernel never keyword-guesses intent"),
so it must be reviewed as a deliberate, scoped exception, not slipped in. It is
intentionally left out of this PR.

The intended integration points, once that review is scheduled:

1. **Greeting / help short-circuit** -- in `Kernel.process_event`, after binding
   resolves (`resolved`/`agent_id` are known) and after the kill-switch gate,
   before the retry loop:

   ```python
   packs = self._binding.packs_for(resolved)
   reply = match_greeting(packs, qevent.text) or match_help(packs, qevent.text)
   if reply is not None:
       await self._drop_with_message(qevent, reply)   # edits placeholder, marks done
       return
   ```

   This replies from the already-posted placeholder and never claims a sandbox
   or calls the model -- a strict cost win on trivial turns. It runs only for a
   *new* turn (a follow-up mid-thread is still a steer; do not short-circuit a
   thread with a live turn).

2. **Load/tips first-edit** -- in `Kernel._consume`, seed the `_ThrottledReply`
   with the sampled load line (optionally plus a tip) so the dispatcher's generic
   placeholder is replaced by the per-agent line before the first `text_delta`
   arrives. (This is also the surface the shimmer status can draw from instead of
   generic text, once wired.)

   ```python
   load = sample_load(packs, qevent.thread_ts)
   tip = sample_tip(packs, qevent.thread_ts)
   opener = "\n\nTip: ".join(x for x in (load, tip) if x) or None
   if opener is not None:
       await self._sink.update(channel=..., ts=qevent.placeholder_ts, text=opener)
   ```

   `packs` must be threaded from `process_event` (where binding resolved) into
   `_attempt`/`_consume`; keep the sampler call out of the streaming hot path
   (compute the opener once, before the stream loop).

Both call sites consume only `behaviorpacks` pure functions and the binding
already built here, so the F1 diff is small and free of new control-flow races.

## Every template battery, mapped to this mechanism

The packs mechanism was designed against the full set of "all-agent" batteries in
the CurieTech agent templates (`agent-ss-template`, `agent-mcp-template`,
`agentkit`, and the newer batteries in `revenue-leak-agent`). The point of packs
is the *declarative, per-agent, opt-in* subset. The table records where each
battery lands, because "make everything a pack" is the wrong goal: a pack is data
consumed by platform code, so a battery that is itself code, or that AgentOS
already provides, or that needs a reply model AgentOS does not have, is not a
pack.

| Battery | Disposition | Why |
|---|---|---|
| Working status (load lines) | **Pack** (`load`) | Pure data (rotating load lines) sampled by a platform function. |
| Capability tips | **Pack** (`tips`) | Pure data (rotating tips), sampled independently of load lines. |
| Greeting detection | **Pack** (`greeting`) | Data (phrases + reply), deterministic pre-model matcher. Shipped. |
| Help / "what can you do" | **Pack** (`help`) | Same shape as greeting; the niceties battery's help half. Shipped. |
| Runtime settings / knobs | **Pack** (`settings`, schema) | The editable-settings allowlist is declarative; shipped with platform-owned validation (`coerce_setting`/`resolve_settings`). The durable override store + edit UI are a deferred runtime. |
| Kill switch / control | **Already native** | AgentOS has it: `apps/worker/src/agentos_worker/killswitch.py::KillSwitch` + `Kernel` kill gate + the `/agents/{id}/kill` control endpoint. A pack would duplicate it. |
| Activity log | **Already native (observability)** | Post-model observation is Langfuse tracing; the Slack "activity" ring buffer is stateful UI code, not declarative data. |
| Logging setup | **Not per-agent** | Pure infra, identical for every agent; belongs to the platform, not a pack. |
| Block Kit rendering | **Not applicable** | AgentOS streams text + `chat_update` mrkdwn; there is no structured `Reply` object to render. Would need a Block Kit reply model first. |
| Navigation (back/up) | **Pack** (`nav`) | The hub button is declarative (label + command); `ensure_hub_button` is the platform-owned no-dead-ends policy over a reply's buttons. Viable now that buttons render + respond; applying it during render is the deferred wiring. |
| HTTP backoff, vision, polling-worker | **Runner/plugin code** | These are agentkit *libraries*. As packs they would run battery code in the worker, breaking sandbox isolation. They belong in the runner/plugin. |
| Integrations (Jira/NetSuite/CPQ) | **MCP servers** | External-system connectors are the plugin's `.mcp.json` surface in AgentOS, not declarative UX data. |
| Proactive notifier / digest | **Domain logic** | Revenue-leak's alerting policy; not a reusable battery. |

The throughline: the moment a battery needs to *run agent-authored logic*, it
stops being a pack and must live in the sandboxed runner (or already lives in the
platform). Packs deliberately cover only the declarative, deterministic slice --
that is what keeps them safe to resolve and apply outside the sandbox.
