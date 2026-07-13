# INTERFACE: Triggers

> Part of the AgentOS swappable-seam catalog — see the [seam index](../../interfaces.md).
> **Kind:** SOFT &nbsp;·&nbsp; **Implementations today:** 2 &nbsp;·&nbsp; **Swap-readiness grade:** not separately graded

**Kind legend:** CLEAN = a real `Protocol`/typed port class · SOFT = swap via env/URL/prefix/wire, no code interface · NONE = not built yet.

## The black line

A "trigger" is the thing that wakes an agent: an inbound event that gets turned into a
run. Today there are **two hardcoded triggers** wired directly into their respective
ingress handlers, with **no shared `Trigger`/`EventSource` port** between them. There
is no swappable line here yet — each trigger is bespoke code. The open architectural
question (Epic #29) is whether "trigger" is even a real seam, or whether new triggers
are just new *event types* handled inside the existing Slack-dispatcher and
API-webhook ingresses. This file records the current state honestly; it does not
assert a port that does not exist.

## Current contract

There is no cross-trigger contract to satisfy — a new trigger today means adding
another hardcoded handler. The two that exist:

- **Slack mention** — `apps/dispatcher/src/agentos_dispatcher/handlers.py:196`:
  `@app.event("app_mention")` on the Slack Bolt app, which calls `process_event(...)`
  to enqueue a run. (An adjacent `@app.event("message")` DM handler sits at
  `handlers.py:208`, gated to `channel_type == "im"`.)
- **GitHub push** — `apps/api/src/agentos_api/routers/github.py:20-21`:
  `@router.post("/webhook")` verifies the HMAC signature, then branches on
  `x_github_event`; a `"push"` event (`github.py:38`) is handed to `process_push(...)`,
  everything else is `"ignored"`.

The two share no abstraction: one is a Slack Bolt event listener, the other a FastAPI
route with GitHub HMAC auth. They converge only downstream (both end up enqueuing work).

**Declaration vs. consumption (#273/#270).** The bundle manifest now carries deploy-time-validated
`triggers` declarations (`cron` with a `schedule`, `webhook` with a `path`; `TriggerDeclaration` in
`packages/plugin-format`, `triggers.*` validation codes), so an agent's non-chat wake-ups ship in one
reviewable artifact and a malformed declaration is rejected at deploy. This is the *declaration*
surface only — the *runtime* that acts on a declared trigger (a cron scheduler, a per-agent webhook
ingress) is still the open Epic #29 question above and is not built. Declaring a trigger validates its
shape; it does not yet wire a live wake-up.

## Implementations today

Two, both hardcoded, in two different processes:

1. Slack `app_mention` in the dispatcher (`handlers.py:196`).
2. GitHub `push` webhook in the API (`routers/github.py:20`).

## Known leakage

The whole seam is "leakage" in the sense that nothing is abstracted yet. Each trigger
carries its source's shape end to end: Slack triggers are Bolt-event-shaped and
authed by the Slack app token; the GitHub trigger is HMAC-signature-shaped and lives
"outside the X-API-Key dependency" (`github.py` docstring). A future `Trigger` port —
if Epic #29 concludes one is warranted — must reconcile these two auth models and
payload shapes into a common event contract, and would live alongside the ingress
handlers rather than replacing the transport-specific receivers.

## Cross-links

- **Epic(s):** #29 — triggers: decide whether "trigger" is a real seam (extract an `EventSource` port) or just new event types on the existing ingresses.
- **Vision doc:** [architecture-vision.md](../../architecture-vision.md) — not one of the six swappable jobs; not separately graded.
- **ADR(s):** none yet — no accepted ADR governs the trigger seam.
