# 39. Bounded delivery: a delivery cap and a dead-letter graveyard

Date: 2026-07-16

Status: Accepted

Implements [#505](https://github.com/curie-eng/curie/issues/505).

**Amends ADR-0013** ("Concurrency and delivery: at-least-once streams with an
idempotent, side-effect-aware kernel"). ADRs are immutable once Accepted, so this
is a new ADR that **supersedes in part** exactly one clause of 0013's Decision:
"Crash recovery without backlog replay: pending entries are reclaimed with
`XAUTOCLAIM`", which as built meant *reclaimed forever, with no cap*. Everything
else in 0013 stands unchanged: at-least-once transport, the idempotent kernel,
dispatcher-side dedupe, the thread-lock routing CAS, the finish race, the
escalate-instead-of-retry rule after a side-effect flag, create-at-`$` so a cold
worker never replays backlog, and the four sacred kernel invariants. This ADR
bounds *how many times* the reclaim clause may fire, and nothing else.

## Context

ADR-0013 chose at-least-once transport plus an idempotent kernel, and made
`XAUTOCLAIM` reclaim the crash-recovery mechanism: an entry a dead consumer took
but never acked is reclaimed after an idle timeout and reprocessed. That loop was
**unbounded** by design. Nothing in the model gave a permanently-failing entry a
terminal state.

Issue #505 is what that costs. `curie local message` starts an ephemeral reply
stub on `localhost:8155` and hands its URL to the turn. When the turn hits an
approval gate, that URL is persisted into a durable `Approval` record and outlives
the CLI process that served it. On resume the worker POSTs the reply to a port
that no longer has a listener, the reply raises a connection error, the error
propagates out of the escalation path, the entry stays pending, and every reclaim
tick re-dispatches it into the same guaranteed failure. The entry never leaves the
pending list. It consumes reclaim budget on every pass forever, and the shared
`curie-workers` consumer group stops making progress on later turns. The failure
is silent: no crash, no alert, just a worker that has quietly stopped working.

The dead CLI stub is only the cause that surfaced first. Any permanently-failing
entry has the same shape: a malformed payload the kernel can never parse, a reply
endpoint that has been decommissioned, a dependency that is gone for good. The
severity does not come from the cause. It comes from the delivery model having no
answer to "what if this never succeeds."

Two adjacent facts frame the fix. First, Valkey's pending-entries list already
tracks a per-entry delivery count in Valkey itself, so the information needed to
bound the loop exists and is durable. Second, the existing unparseable-entry
branch already acknowledged the problem in miniature: it acked poison away
**silently**, because a poison message must not be reclaimed forever. That was the
right instinct with the wrong terminal state, and it deleted the evidence.

## Decision

**Delivery is bounded.** An entry that has already been delivered `max_delivery`
times and still failed is moved to a dead-letter stream and acked off the main
group, rather than reclaimed and re-dispatched again.

- **The cap.** `WorkerConfig.max_delivery` (env `CURIE_MAX_DELIVERY`, default
  `5`, floor `ge=2`). The floor is a hard constraint, not a suggestion:
  `max_delivery=1` would dead-letter every ordinary worker crash on its first
  reclaim, which is precisely the crash recovery ADR-0013 exists to provide.
  Values below 3 undermine that recovery and the field docstring says so.
  `max_delivery` is deliberately **not** `max_attempts`: that existing knob
  governs the kernel's flag-clean per-turn retry *classification* inside a single
  delivery. Conflating the two would silently change kernel retry behavior.
- **The graveyard.** `WorkerConfig.dead_letter_stream` (env
  `CURIE_DEAD_LETTER_STREAM`), empty by default, which derives `<stream>:dead`
  at the use site. A static field default cannot reference `self.stream`, so the
  derivation lives in the consumer's `_dead_letter_stream` property. The first
  `XADD` creates the stream; nothing pre-creates it.
- **The row.** A dead-lettered entry is `XADD`ed with its original fields kept
  verbatim plus namespaced failure metadata: `dl_original_id`,
  `dl_delivery_count`, `dl_reason`, `dl_dead_lettered_at`. The `dl_` prefix means
  the metadata can never collide with a field of the original payload. One
  scheme, no JSON nesting alternative. An id can be pending while its message was
  trimmed off the source stream, in which case the row is metadata-only and the
  `XACK` still happens; skipping it would leave the stall in place.
- **The ordering.** `XADD` precedes `XACK`, deliberately. A crash between the two
  leaves the entry pending, so it is re-reclaimed and re-dead-lettered: a
  duplicate graveyard row. The reverse ordering's failure mode is a lost entry.
  A duplicate row is strictly the cheaper loss. Two replicas racing the same
  over-cap entry produce the same acceptable duplicate, and we do not add locking
  to prevent it.
- **The count comes from the pending-entries list, never from process memory.**
  It is read fresh from Valkey on every reclaim pass. This is load-bearing: a
  process-local counter resets on restart, so a crash-looping worker would rearm
  the budget on every restart and retry poison forever, which is the exact stall
  shape the cap exists to end. A durable counter means a restarted or replacement
  worker sees the accumulated count and still caps.
- **Read the count before the claim, not after.** `XAUTOCLAIM` increments the
  counter as it claims, so the pre-claim value is the number of deliveries
  *already made*. An entry at `>= max_delivery` has had its full budget and must
  not be claimed again. The `IDLE` filter matches `XAUTOCLAIM`'s `min_idle_time`
  so both see the same candidate set and an entry that is not yet
  reclaim-eligible is never dead-lettered early. An entry in flight on this
  consumer is skipped ahead of the cap check: it is being worked right now, not
  orphaned.
- **The unparseable path routes to the same graveyard**, with
  `dl_reason="unparseable"`, replacing the silent ack. Poison becomes observable
  instead of vanishing.
- **Transient failure is unchanged.** A worker that crashes mid-turn still has its
  entry reclaimed and retried, exactly as ADR-0013 specifies. Only an entry that
  has burned its whole budget is dead-lettered.
- **The graveyard has no consumer group, but it IS bounded.** It is a sink, not a
  second processing lane. Replay, if an operator ever needs it, is `XRANGE` plus a
  re-`XADD` onto the main stream. Every `XADD` passes an approximate `MAXLEN` of
  `WorkerConfig.dead_letter_maxlen` (env `CURIE_DEAD_LETTER_MAXLEN`, default
  `10000`, floor `ge=1`). The bound is not optional polish: the unparseable path
  dead-letters **per inbound entry**, so a wire-DTO drift that made entries
  unparseable en masse would grow the graveyard at full ingest rate, on the same
  Valkey that holds the kernel's per-thread locks and side-effect markers. An
  unbounded graveyard would therefore introduce a platform-wide OOM vector that
  did not exist before this change (previously an unparseable entry was acked and
  dropped for free). `approximate=True` lets Valkey trim on node boundaries, so
  the stream is bounded at *at least* the configured length, not exactly it.
- **A self-targeting graveyard is rejected at config validation.**
  `WorkerConfig._reject_self_targeting_graveyard` fails construction when an
  explicit `dead_letter_stream` equals `stream`. Because `_dead_letter` XADDs
  before it XACKs, a graveyard pointing at the source stream re-queues every
  failure onto the stream it was consumed from, and an unparseable entry hot-loops
  there, recreating the exact permanent stall the cap exists to end. The derived
  `<stream>:dead` default can never collide, so only an explicit override trips
  this. An operator learns at boot rather than during an incident.

**Three verbs are added to the consumer's `StreamBroker` port**
(`xpending_range`, `xrange`, `xadd`), taking it from four to seven. This is
consistent with ADR-0027, not an exception to it: the split that ADR draws is
about **dedupe placement** living on the producer side, not a rule that consumers
never write. A dead-letter append is consumer-group lifecycle, the same family as
`xack` and `xautoclaim`. Declaring `xadd` on the port is what makes the contract
honest; hiding the write behind a cast would let a second broker ship structurally
satisfying the port while being silently unable to dead-letter, which is the
failure the port exists to prevent.

## Alternatives considered

- **Retry forever (the status quo this closes off).** ADR-0013 chose an unbounded
  reclaim loop for a real reason: crash recovery must not lose an entry, and any
  cap is a decision to eventually stop trying. That reasoning holds for the
  transient case and we keep it there. It is wrong for the permanent case. A
  permanently-failing entry, such as one whose reply endpoint died with the CLI
  process that created it, never converges. Under an unbounded loop it starves the
  shared consumer group and silently stalls every later turn (#505). "Never lose
  an entry" was traded against "never stall the group," and the ADR-0013 model
  only priced the first. Bounded delivery keeps the entry (in the graveyard) and
  ends the stall.
- **Fix only the CLI stub lifecycle (Option 1).** Make `local message` detect the
  approval-gated case and warn that its reply stub is ephemeral. Rejected as the
  fix: it reduces how often *this one cause* fires and does nothing for any other
  permanently-failing entry. The group can still stall. Deferred as
  [#529](https://github.com/curie-eng/curie/issues/529), a needed follow-up and
  not a substitute.
- **Fix only the resume transport fallback (Option 2).** Fall back to the worker's
  configured default transport when a per-turn reply endpoint is unreachable.
  Rejected as the fix for the same reason, plus it risks mis-delivering a reply to
  the wrong workspace and needs its own design. Deferred as
  [#530](https://github.com/curie-eng/curie/issues/530), a needed follow-up and
  not a substitute. Neither #529 nor #530 replaces the cap: they reduce how often
  the CLI-stub case fires and do nothing for the general poison case.
- **Track the delivery count in the worker process.** Rejected: a process-local
  counter resets on restart, so a crash-looping worker would never reach the cap.
  It would produce exactly the silent stall the cap exists to end, while looking
  like it had a cap.
- **Build a dead-letter consumer or replay tool now.** Rejected: no operator has
  needed one. The graveyard's value is that the entry is durable and observable;
  a processing lane on top of it is a separate decision to make when there is
  demand to verify against.
- **Leave the graveyard unbounded.** Rejected. The `MAXLEN` is not a retention
  policy bolted on as a passenger; it is the containment for a vector this change
  itself introduces. Routing the per-inbound-entry unparseable path into a durable
  stream converts a free drop into a write, so an unbounded graveyard hands a
  wire-DTO drift a full-ingest-rate OOM against the Valkey the kernel's own
  correctness depends on. A retention *policy* (age-based expiry, tiering, replay
  tooling) remains undecided and is not a passenger on this change.
- **Exempt side-effect-flagged entries from the cap.** Considered and rejected. It
  is tempting to say an entry whose work already happened must never be given up
  on. But the motivating #505 shape, a resume turn POSTing to a dead reply
  endpoint, can itself carry the side-effect flag, so the exemption would apply to
  exactly the entry that can never succeed and would resurrect the infinite loop
  the cap exists to end. The side-effect marker survives in Valkey either way.

## Consequences

- **The honest trade: an entry that is dead-lettered may never deliver its reply,
  and its side effects may already have happened.** In the #505 case the resumed
  turn did its work; only the reply POST fails. Dead-lettering means that work is
  done and the reply is lost, and the user is not notified. This is a deliberate
  severity reduction, not an elimination: from a **silent total stall of the
  worker group** to a **single-turn loss** with a graveyard row and a loud error
  log naming the entry, its delivery count, and the reason. It is also exactly why
  #529 and #530 remain needed follow-ups rather than discarded alternatives. They
  attack the frequency of the loss this change makes survivable.
- **"Observable" is only half earned today.** A dead-letter emits an ERROR log and
  writes a graveyard row, so the evidence exists and a human who goes looking will
  find it. But **no alarm, no metric, and no consumer watches `<stream>:dead`**.
  Nothing pages, and nothing surfaces the row to anyone who is not already
  looking. The severity trade above is therefore "silent total stall" against
  "logged single loss," which is a real improvement and still short of observable
  in the operational sense. Closing that gap is
  [#531](https://github.com/curie-eng/curie/issues/531).
- **A dead-lettered RESUME turn strands its approval.** `ResumeQueue.enqueue`
  (`apps/api/src/curie_api/resumequeue.py`) sets `resumed_at` once the XADD
  succeeds, and the resume reconciler
  (`apps/api/src/curie_api/resumereconciler.py`, over `list_resolved_unresumed`
  in `apps/api/src/curie_api/crud.py`) is gated on `resumed_at IS NULL`. A turn
  that reached the stream and then died is therefore marked resumed and has no
  backstop: approved in Postgres, sandbox never wakes, nothing retries it. This is
  **not a regression**. Before this change the same turn was reclaimed forever
  against a dead endpoint, never resumed either, and stalled the whole group while
  failing. Bounding delivery does not create the stranding; it makes a
  pre-existing gap visible and localizes it to one turn. Closing it is
  [#532](https://github.com/curie-eng/curie/issues/532).
- **A cap is now a load-bearing invariant, and removing it is a regression, not a
  simplification.** This inherits ADR-0013's warning about the retry loop: a
  future change that drops the cap, raises it to infinity, or moves the count into
  process memory reintroduces #505's silent stall and will pass unit tests while
  doing it. The floor of `ge=2` is a config-level guard against the opposite
  mistake.
- **`kernel.py`, `threadlock.py`, and `markers.py` are untouched.** The cap lives
  at the delivery layer in `consumer.py`. Rule 4 (no auto-retry after side
  effects) holds *within* every delivery: the persisted side-effect flag makes
  each reclaim escalate rather than retry, and an entry only reaches the cap when
  escalation **itself** keeps failing. Dead-lettering after N failed escalations
  is that path's correct terminal state, not a violation of it: the only
  alternative is infinite failed escalations, which is the stall. A cap exemption
  for side-effect-flagged entries was considered and rejected, because the
  motivating #505 shape can itself carry the flag and exempting it would resurrect
  that exact infinite loop. The side-effect marker survives in Valkey regardless
  of the cap. A dead-letter never dispatches, never acquires the semaphore, and
  never touches the thread lock.
- **Graveyard records are best-effort, not a durable audit log.** The approximate
  `MAXLEN` evicts the oldest rows under a flood, which means the failures most
  worth reading (the first ones, at the onset of an incident) are the first
  evicted once the cap is reached. Bounded record loss is deliberately traded
  against a platform-wide OOM: losing some dead-letter rows costs evidence, while
  an unbounded graveyard costs the Valkey the kernel's locks and markers live in,
  and with it the whole platform. Do not build anything that treats the graveyard
  as complete. Below the cap it holds every dead-lettered entry, and its length
  remains a read on the system's poison rate, saturating rather than growing once
  the bound is hit.
- **`StreamBroker` is a seven-verb port.** A second broker must implement the
  dead-letter verbs to drop in. ADR-0027's remaining coupling note still applies;
  this widens the contract without widening the coupling.
