# 29. The conversation-history port and its first loader: transcript replay over the durable state store

Date: 2026-07-14

Status: Accepted

Implements transcript persistence across unplanned runner restarts for issue
[#20](https://github.com/curie-eng/agentos/issues/20). This is the first loader
for `AGENTOS_HISTORY_REF`, the runner-local rehydration ref that until now was
read into `RunnerConfig` and fed to the SDK `resume=` option but, in practice,
never carried a usable value. It is the sibling of ADR-0025 (the memory port and
its first loader): same store, same boot-preamble delivery, a different scope
(per-thread conversation vs per-agent memory).

## Context

Sessions are stateless-first (ADR-0003): a suspended or restarted sandbox is a
new pod, and its in-pod SDK transcript (on emptyDir) is gone. Today an unplanned
runner-pod death mid-thread loses the conversation: the worker's route stays
`LIVE` with no history ref, the next claim cold-boots a fresh, history-less
sandbox, and the user is silently talking to an amnesiac agent. The
suspend/resume path that was supposed to rehydrate is also unreached in
production (nothing suspends, nothing produces a resume id) and, even if it were,
it pointed the SDK `resume=` at transcript files that died with the pod.

Two constraints shape the fix:

- **History must live outside the sandbox and be harness-agnostic.** ADR-0011 and
  ADR-0021 commit us to more than one coding-agent harness (OpenCode alongside
  claude-agent-sdk). An SDK-native resume id is claude-agent-sdk-specific and
  would have to be redone per harness; the rehydration mechanism must not assume
  any one harness's transcript format or resume API.
- **We already have the durable store this needs.** ADR-0025 landed an
  agent-scoped, namespaced, Postgres-JSONB state store with a log-shaped `append`
  endpoint and per-value/per-namespace size caps, reachable from the sandbox, and
  used it to deliver agent *memory* as a boot-time system-prompt preamble.
  Conversation history is the same shape of problem (durable, external,
  rehydrated at boot) at a different scope.

## Decision

**Persist the per-thread conversation transcript as a scoped namespace over the
existing state store, behind a small `TranscriptStore` port, and deliver it to a
(re)started runner as a boot-time system-prompt preamble.** This is Option B
(platform-owned, harness-agnostic replay), chosen over Option A (SDK-native
resume).

- **The port** (`runner/src/agentos_runner/history.py`) is a `Protocol` with two
  methods, `load() -> list[TurnRecord]` and `append(record)`. A `TurnRecord` is a
  `user` message plus the `assistant` reply (and a `ts`). No query language and
  no summarization -- windowing/compaction is later work.
- **The default backing** is `StateApiTranscriptStore` over the ADR-0025 state
  store: the thread's transcript is the log-shaped key
  `/agents/<agent_id>/state/transcript/<thread_key>`. `load` GETs the key (404 =
  no prior turns), `append` POSTs one turn to the key's `/append` endpoint.
  `NullTranscriptStore` is used when no ref is configured, so the boot path is
  uniform. Durability, size caps, and survive-restart come from the state store
  for free, with no new datastore to operate.
- **`AGENTOS_HISTORY_REF` is repurposed as the transcript-namespace URL**, exactly
  as ADR-0025 did for `AGENTOS_MEMORY_REF`: an `http(s)://` ref resolves to the
  `StateApiTranscriptStore`; any other scheme (an old SDK-resume id, `s3://`) is
  rejected loudly at boot. It is a runner-local env, never part of the frozen ACI
  `SessionConfig`, so this is **not a frozen-contract change**. The state-API
  bearer travels as the runner-local `AGENTOS_HISTORY_TOKEN`.
- **The runner stops feeding `history_ref` to the SDK `resume=` option.** The
  harness-specific resume path is retired in favor of the harness-agnostic
  preamble; `build_options` keeps its `resume` parameter (an explicit caller may
  still use it) but the boot path no longer wires history into it.
- **Delivery into the sandbox:** at boot the store is resolved, prior turns are
  loaded, and they are composed into the effective system prompt as a
  conversation preamble (leading the prompt alongside the memory preamble), so a
  restarted session sees the prior exchange as context. **Every claim for a thread
  injects the same deterministic ref**, so a fresh, a restarted, and a resumed
  sandbox all rehydrate identically -- the unplanned-restart case needs no special
  worker/kernel branch.
- **The write side** is the runner: after each turn reaches a successful terminal
  `final`, it appends `{user, assistant}` to the transcript store. Best-effort --
  a transient store failure logs and never fails the turn (and boot degrades to
  "no history" rather than blocking, like memory).
- **The worker delivers the ref:** `binding.boot_env` sets `AGENTOS_HISTORY_REF`
  to the thread's transcript-namespace URL and forwards the API key as
  `AGENTOS_HISTORY_TOKEN`, mirroring the memory-ref wiring.

## Alternatives considered

- **Option A -- SDK-native resume.** Surface the claude-agent-sdk `session_id` on
  the ACI `Final` frame, persist the SDK's own transcript files durably, restore
  them into the new pod, and `resume=<id>`. Rejected: it locks rehydration to
  claude-agent-sdk (breaks with the ADR-0011/0021 multi-harness direction),
  requires a frozen-contract change to carry the id, and couples our durable
  store to the SDK's internal transcript file format. It buys the SDK's exact
  prompt-cache continuity, but ADR-0003 already treats cache warmth as lost across
  a restart, so that is not a benefit we are giving up.
- **A new S3/object-store transcript backend.** Rejected for the same reason
  ADR-0025 rejected it for memory: a second datastore to operate when the state
  store already gives durability, agent-scoping, namespacing, size caps, and an
  append log.
- **Persist in Valkey (the affinity store).** Rejected: Valkey is the routing
  store, not the durable state of record; the state store is the adopted spine for
  cross-turn durable state, and reusing it keeps one durability story.

## Consequences

- An unplanned runner restart mid-thread rehydrates the conversation: the fresh
  sandbox loads the thread's transcript and leads the system prompt with it. No
  new infrastructure, no frozen-contract change, no sacred-kernel change; the
  worker change is one `boot_env` block mirroring memory.
- History and memory are distinct namespaces over one store: `transcript/<thread>`
  (this conversation) vs `memory` (durable cross-session lessons). The runner
  reinterpretation of `AGENTOS_HISTORY_REF` (URL, not SDK resume id) supersedes the
  old `config.py` framing; `runner/CLAUDE.md` is updated to match.
- **Known limitations (follow-up):** the transcript is an unbounded append log
  under the state store's size caps, so a very long thread will eventually hit the
  cap; windowing/summarization of the replayed context is deferred. The replay is
  a prompt preamble, not a token-exact reconstruction of the prior context, so it
  does not restore the model's original prompt cache (consistent with ADR-0003).
  The state API has one shared key today, so forwarding it as the history token
  grants the sandbox that key's scope -- the same auth-hardening follow-up ADR-0025
  notes. Live proof of the pod-death-then-rehydrate path needs a real cluster; the
  load-as-preamble mechanism is verified at boot without killing a pod.
