# CLAUDE.md - apps/worker

The concurrency kernel plus the Agent Sandbox substrate. The eval runner is not
yet built. Full behavior spec lives in `apps/worker/README.md`; this file is the
enforceable-rule summary. Read `../../ARCHITECTURE.md`'s message-flow diagram
before changing `kernel.py`.

## The kernel is sacred: single owner, adversarial review

`kernel.py`/`consumer.py`/`threadlock.py`/`markers.py` are correctness logic
with races that only show up under concurrent load. **One change owns this
module at a time, it is never split across parallel work, and any change needs
an adversarial review (spec-vs-impl + side-effects-detective, minimum) before
merge.** If you are touching `kernel.py` as a side effect of another change,
stop -- that is scope creep on the sacred module.

## The four rules the kernel enforces (each has a provoking integration test)

1. **One live session per thread.** A follow-up to a thread with a live turn
   is a *steer*, never a new turn. The per-thread lock (Valkey `SET NX PX`
   plus an in-process FIFO lock) wraps the route decision; the new turn
   opens *before* the lock releases. The lock is never held during
   streaming -- if you find yourself holding it longer, you broke "steering
   is never blocked."
2. **The finish race.** A steer arriving as the turn ends returns 409; the
   kernel opens a fresh turn on the same idle sandbox. Do not "fix" a 409 by
   retrying the steer instead of falling back to a new turn.
3. **Steer vs. interrupt.** Default is steer; `Kernel.interrupt_thread` is
   the explicit hard stop. **The kernel never keyword-guesses intent** -- do
   not add text-sniffing to decide between the two; that is an explicit
   caller signal.
4. **No auto-retry after side effects.** A failed run that emitted
   `side_effect_flag` escalates to a human instead of retrying; the flag is
   persisted to Valkey the instant it is seen so a crash mid-side-effect
   still escalates on reclaim. Flag-clean failures retry by classification
   (`rate-limit`/`runner-error` transient, everything else escalates).

## The sandbox substrate (`agentos_worker.sandbox`)

- **The kernel talks in `thread_key` and `SandboxHandle` only.** Everything
  Kubernetes-shaped stays behind the `SandboxSubstrate`/`SandboxClient` seam
  -- never leak a K8s type across it into the kernel.
- **`claim()` is claim-or-adopt.** A lost creation race deletes the loser's
  claim; the route lives in Valkey (`agentos:sandbox:route:<thread_key>`)
  with a TTL that `claim()`/`touch()` refresh.
- **Claims carrying per-claim env (the resume path) cold-create** instead of
  binding the warm pool, because env cannot be injected into an
  already-running pod. Expected latency, not a bug.
- **Suspend deletes the pod.** Never assume a suspended sandbox's process or
  prompt cache survives; a resumed or restarted sandbox rehydrates from external
  state via `AGENTOS_HISTORY_REF`.
- **The client seam is sync; unit tests fake only `SandboxClient`** (the K8s
  control plane) -- Valkey is never mocked.
- **The history ref is a deterministic state-store URL, not an SDK session id**
  (ADR-0029). `binding.boot_env` sets `AGENTOS_HISTORY_REF` to this thread's
  transcript key on the durable state store (`.../state/transcript/<thread_key>`)
  on **every** claim, so a fresh, restarted, or resumed sandbox all boot with the
  same ref and the runner rehydrates the conversation as a preamble. There is no
  SDK session id to capture off the ACI `final` frame and no frozen-contract
  change; do not reintroduce a frame-captured resume id.

## Deployment-to-runtime binding (implemented)

`binding.py` resolves a thread's Slack channel to its agent, that agent's active
deployment (prod outranks dev, then most recent), and the resolved
`AGENTOS_BUNDLE_REF`, and injects it into the sandbox claim so the boot picks up
the bundle version the API's git-flow engine produced. It is a read-only query
layer over the shared Postgres tables (agents -> deployments -> agent_versions),
deliberately not an import of the API package. The seam to remember: a thread
keeps the sandbox and bundle it first booted with; only a fresh mention (a new
claim) picks up a newer deployment. Any change that touches the kernel's claim
path here is still bound by the sacred-module rule above.

## Verify

```bash
uv run pytest apps/worker/tests/kernel -q     # real Valkey, fake K8s client, in-process fake runner, recording Slack sink
uv run pytest apps/worker/tests/sandbox -q    # unit; AGENTOS_SANDBOX_E2E=1 additionally drives a real cluster
```

Only Slack and the model behind the runner are faked in the kernel suite.
