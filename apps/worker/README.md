# apps/worker

Owning tasks: **F1** (the prod-hard concurrency kernel: routing rule, finish-race CAS, steer/interrupt, no-retry-after-side-effects, resume-rehydrate), **G1** (Agent Sandbox substrate module: warm pool, thread-to-sandbox affinity, claim/release), **K1** (eval runner module). F1 is single-owner and never split, with an escalated adversarial review roster. Reads Valkey Streams via redis-py consumer groups; drives claimed sandboxes running the D1 runner image.

## F1: the concurrency kernel (`agentos_worker.kernel` + `agentos_worker.consumer`)

The kernel closes the loop: it consumes `QueuedSlackEvent` entries the dispatcher
puts on the `agentos:runs` Valkey stream, routes each to a runner turn in a
claimed sandbox, streams the NDJSON reply back into the Slack thread by editing
the placeholder in place, and gets every failure mode right.

```
XREADGROUP agentos:runs        Consumer (consumer group; XAUTOCLAIM reclaims a
   -> QueuedSlackEvent            dead consumer's pending entries after an idle
   -> Kernel.process_event        timeout, then reprocesses them idempotently)
        -> substrate.lookup/claim/resume   (G1)
        -> RunnerClient  POST /v1/event | /v1/steer | /v1/interrupt   (D1)
        -> SlackSink     chat.update the placeholder as frames stream
   -> XACK
```

Rules (detailed-architecture 2b), each with an integration test that provokes it
(`tests/kernel/`):

- **One live session per thread.** A follow-up to a thread with a live turn is a
  *steer* (`POST /v1/steer`) into that turn, not a new turn. The per-thread lock
  (Valkey `SET NX PX` across workers, plus an in-process FIFO lock for arrival
  ordering) wraps the route decision, and the new turn is opened *before* the
  lock is released, so a concurrent follow-up can only steer, never fork a second
  turn. The lock is not held during streaming, so steering is never blocked.
- **The finish race.** A steer that arrives as the turn ends returns 409; the
  kernel then opens a fresh turn on the same idle sandbox. This check-and-fall-
  back is the compare-and-swap the worker owns.
- **Steer vs interrupt.** Default is steer. `Kernel.interrupt_thread` is the
  explicit hard stop (`POST /v1/interrupt`), which a Slack `:stop:` affordance
  would call; the kernel never keyword-guesses intent.
- **No auto-retry after side effects.** A failed run that emitted
  `side_effect_flag` escalates to a human (the placeholder is edited to say so)
  instead of retrying. The flag is persisted to Valkey the instant it is seen, so
  a worker crash mid-side-effect still escalates on reclaim rather than re-running
  a non-idempotent action. Flag-clean failures retry by classification:
  `rate-limit` and `runner-error` are transient (bounded exponential backoff);
  `budget-exceeded` and everything else escalate.
- **Idempotency + crash recovery.** The Slack event id gates a `done` marker, so
  a redelivered or reclaimed entry that already finished is skipped.
  `XAUTOCLAIM` reclaims entries a dead consumer took but never acked and
  reprocesses them; the markers make that safe.

Config surface (`WorkerConfig.from_env`): `VALKEY_*`, `SLACK_BOT_TOKEN`,
`AGENTOS_STREAM` / `AGENTOS_CONSUMER_GROUP` / `AGENTOS_CONSUMER_NAME`,
`AGENTOS_MAX_ATTEMPTS`, plus `AGENTOS_NAMESPACE` / `AGENTOS_WARM_POOL` /
`AGENTOS_RUNNER_PORT` for the substrate. Run with `python -m agentos_worker`.

Tests: `uv run pytest apps/worker/tests/kernel -q` runs against the real Valkey
from `compose.dev.yaml`, the real G1 substrate with a fake Kubernetes client whose
sandboxes resolve to a local in-process fake runner, and a recording Slack sink.
Only Slack and the model behind the runner are faked.

## G1: the sandbox substrate (`agentos_worker.sandbox`)

The lifecycle seam between the worker kernel and kubernetes-sigs/agent-sandbox
v0.5.0 (core `Sandbox` CRD + the extensions `SandboxClaim`/`SandboxWarmPool`/
`SandboxTemplate`). F1 talks in `thread_key` (the Slack `thread_ts`) and
`SandboxHandle`; everything Kubernetes-shaped stays behind this module.

```python
from agentos_worker.sandbox import (
    AffinityStore, KubernetesSandboxClient, SandboxSubstrate, SubstrateConfig,
)

substrate = SandboxSubstrate(
    KubernetesSandboxClient(namespace),          # or any SandboxClient impl
    AffinityStore(redis_client),                 # the compose/chart Valkey
    SubstrateConfig(namespace=..., warm_pool="<release>-runner-pool"),
)

handle = substrate.claim(thread_ts)   # existing live route, or warm-pool claim
# handle.base_url -> http://<serviceFQDN>:8080  (dial from inside the cluster)
substrate.suspend(thread_ts, history_ref=sdk_session_id)
handle = substrate.resume(thread_ts)  # new claim, AGENTOS_HISTORY_REF injected
substrate.release(thread_ts)          # delete claim -> sandbox+pod reaped
substrate.reap_orphans()              # periodic tick: claims with no live route
```

Contract notes F1 must know:

- **One live session per thread.** `claim()` is claim-or-adopt: a lost
  creation race deletes the loser's claim and returns the winner's handle. The
  route lives in Valkey (`agentos:sandbox:route:<thread_key>`) with a TTL;
  `claim()`/`touch()` refresh it on activity.
- **Sub-second claims require a warm pool.** The chart's
  `agentSandbox.deploy=true` installs `<release>-runner` (SandboxTemplate) and
  `<release>-runner-pool` (SandboxWarmPool). Claims without per-claim env bind
  a pre-warmed sandbox (0.04-0.07 s measured on k8scratch); claims **with**
  env (the resume path) get a fresh sandbox instead (cold create, seconds not
  sub-second) because env cannot be injected into an already-running pod.
- **Suspend/resume is a cold rehydrate (PT-1).** `suspend()` flips the Sandbox
  to `Suspended` (the pod is deleted) and records the caller-supplied history
  ref. `resume()` retires the old claim and creates a new one whose per-claim
  env injects `AGENTOS_HISTORY_REF` (+ the original `AGENTOS_SESSION_ID`); the
  runner passes that ref to the SDK as its `resume` session id. Never assume
  process or prompt-cache warmth across a suspend.
- **Producing the history ref is the caller's job.** The frozen ACI `final`
  frame does not carry the SDK session id today; if F1 needs it surfaced from
  the runner, that is a contract change to escalate, not to work around.
- **Reaping.** `release()` deletes the claim (the claim owns its sandbox and
  pod). Routes that expire in Valkey leave orphaned claims; `reap_orphans()`
  lists claims labeled `agentos.dev/managed-by=agentos-sandbox-substrate` and
  deletes any not referenced by a live route. Run it on a periodic worker tick.
- The client seam (`SandboxClient` protocol) is sync; wrap calls in a thread if
  the kernel goes async. Unit tests fake only this protocol (the K8s control
  plane); Valkey is never mocked. The env-gated e2e
  (`tests/sandbox/test_e2e_k8scratch.py`, `AGENTOS_SANDBOX_E2E=1`) drives the
  real cluster.
