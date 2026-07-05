# apps/worker

Owning tasks: **F1** (the prod-hard concurrency kernel: routing rule, finish-race CAS, steer/interrupt, no-retry-after-side-effects, resume-rehydrate), **G1** (Agent Sandbox substrate module: warm pool, thread-to-sandbox affinity, claim/release), **K1** (eval runner module). F1 is single-owner and never split, with an escalated adversarial review roster. Reads Valkey Streams via redis-py consumer groups; drives claimed sandboxes running the D1 runner image.

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
  a pre-warmed sandbox (~0.2 s measured); claims **with** env (the resume path)
  force a cold sandbox creation (~2-3 s) because the controller cannot inject
  env into an already-running pod.
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
