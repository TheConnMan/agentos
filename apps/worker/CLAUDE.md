# CLAUDE.md — apps/worker

The concurrency kernel (F1) plus the Agent Sandbox substrate (G1). Owning
tasks: F1 (single owner, never split -- see below), G1, K1 (eval runner, not
yet built). Full behavior spec lives in `apps/worker/README.md`; this file is
the enforceable-rule summary. Read `docs/architecture.md`'s message-flow
diagram before changing `kernel.py`.

## F1 is sacred: single owner, adversarial review

`kernel.py`/`consumer.py`/`threadlock.py`/`markers.py` are correctness logic
with races that only show up under concurrent load. **One agent owns this
module at a time, it is never split across parallel tasks, and any change
needs an escalated adversarial review (spec-vs-impl + side-effects-detective,
minimum) before merge.** If you are touching `kernel.py` as a side effect of
another task, stop -- that is scope creep on the sacred module.

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

## G1: the sandbox substrate (`agentos_worker.sandbox`)

- **F1 talks in `thread_key` and `SandboxHandle` only.** Everything
  Kubernetes-shaped stays behind the `SandboxSubstrate`/`SandboxClient` seam
  -- never leak a K8s type across it into the kernel.
- **`claim()` is claim-or-adopt.** A lost creation race deletes the loser's
  claim; the route lives in Valkey (`agentos:sandbox:route:<thread_key>`)
  with a TTL that `claim()`/`touch()` refresh.
- **Claims carrying per-claim env (the resume path) cold-create** instead of
  binding the warm pool, because env cannot be injected into an
  already-running pod. Expected latency, not a bug.
- **Suspend deletes the pod.** Never assume a suspended sandbox's process or
  prompt cache survives; `resume()` always rehydrates from
  `AGENTOS_HISTORY_REF`.
- **The client seam is sync; unit tests fake only `SandboxClient`** (the K8s
  control plane) -- Valkey is never mocked.
- **Producing a history ref is the caller's job.** The frozen ACI `final`
  frame does not carry the SDK session id today; surfacing it is an
  `aci-protocol` change -- stop and escalate, do not invent a side channel.

## Known gap: deployment-to-runtime binding

The kernel claims a sandbox running a fixed runner image/plugin today -- it
does not resolve a thread's `deployment_id` to the bundle version J1's
git-flow produced. Confirm with the orchestrator before bolting deployment
resolution onto the kernel; this is SK-gate-adjacent work and F1's
single-owner rule applies.

## Verify

```bash
uv run pytest apps/worker/tests/kernel -q     # real Valkey, fake K8s client, in-process fake runner, recording Slack sink
uv run pytest apps/worker/tests/sandbox -q    # unit; AGENTOS_SANDBOX_E2E=1 additionally drives a real cluster
```

Only Slack and the model behind the runner are faked in the kernel suite.
