# Behavior packs

Per-agent, opt-in UX touches applied around a turn, so an agent owner can enable
them for their deployments without imposing them on every other agent on the
install:

- **tips** -- a sampled "working..." line (optionally with a capability tip)
  shown while a turn runs.
- **greeting** -- a canned reply to a *bare* greeting ("hi", "hey there team")
  that never calls the model.
- **help** -- a canned reply to a *bare* help / "what can you do" request, also
  without a model call (the niceties battery's help half).

These three are illustrative, not the point. The point is the mechanism: a
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
- **Logic** (`apps/worker/behaviorpacks.py`): `sample_tip(packs, seed)`,
  `match_greeting(packs, text)`, and `match_help(packs, text)`, pure stdlib,
  fully unit-tested. The two matchers share one bare-utterance core: they return
  a reply only for a phrase said alone (or with trailing filler); a phrase glued
  to a real request ("hi show me the report") returns `None` and falls through to
  the model.
- **Binding** (`apps/worker/binding.py`, not the sacred kernel): the resolver
  now selects `behavior_packs`, carries it on `ResolvedDeployment`, and exposes
  `BindingResolver.packs_for(resolved) -> BehaviorPacks`.

## What is NOT built: the kernel wiring (needs F1 review)

Both touches fire from inside `apps/worker/kernel.py`, which is the F1 "sacred"
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

2. **Tips first-edit** -- in `Kernel._consume`, seed the `_ThrottledReply` with
   the sampled working line so the dispatcher's generic placeholder is replaced
   by the per-agent line before the first `text_delta` arrives:

   ```python
   opener = sample_tip(packs, qevent.thread_ts)
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
| Working status / tips | **Pack** (`tips`) | Pure data (lines + tips) sampled by a platform function. Shipped. |
| Greeting detection | **Pack** (`greeting`) | Data (phrases + reply), deterministic pre-model matcher. Shipped. |
| Help / "what can you do" | **Pack** (`help`) | Same shape as greeting; the niceties battery's help half. Shipped. |
| Runtime settings / knobs | **Candidate pack (schema only)** | The editable-settings allowlist is declarative and could be a pack; the store + live-override + edit UI is a stateful subsystem that overlaps AgentOS budgets/config. Deferred. |
| Kill switch / control | **Already native** | AgentOS has it: `apps/worker/killswitch.py` + `Kernel` kill gate + the `/agents/{id}/kill` control endpoint. A pack would duplicate it. |
| Activity log | **Already native (observability)** | Post-model observation is Langfuse tracing; the Slack "activity" ring buffer is stateful UI code, not declarative data. |
| Logging setup | **Not per-agent** | Pure infra, identical for every agent; belongs to the platform, not a pack. |
| Block Kit rendering | **Not applicable** | AgentOS streams text + `chat_update` mrkdwn; there is no structured `Reply` object to render. Would need a Block Kit reply model first. |
| Navigation (back/up) | **Not applicable** | Presupposes the Block Kit `Reply` model above. |
| HTTP backoff, vision, polling-worker | **Runner/plugin code** | These are agentkit *libraries*. As packs they would run battery code in the worker, breaking sandbox isolation. They belong in the runner/plugin. |
| Integrations (Jira/NetSuite/CPQ) | **MCP servers** | External-system connectors are the plugin's `.mcp.json` surface in AgentOS, not declarative UX data. |
| Proactive notifier / digest | **Domain logic** | Revenue-leak's alerting policy; not a reusable battery. |

The throughline: the moment a battery needs to *run agent-authored logic*, it
stops being a pack and must live in the sandboxed runner (or already lives in the
platform). Packs deliberately cover only the declarative, deterministic slice --
that is what keeps them safe to resolve and apply outside the sandbox.
