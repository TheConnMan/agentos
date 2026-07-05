# Architecture

AgentOS turns a Slack thread into a conversation with a versioned, sandboxed AI
agent, and turns a git push into a deployment of that agent. This document is
the map: the components, how a message flows through them, and how a deploy
flows through them. It is a distillation of the reference spec
(`docs/reference/detailed-architecture.md`), the build plan
(`docs/mvp-build-plan.md`), and the ADRs (`docs/adr/`) — read those for the
"why," this doc for the "what talks to what."

Every claim below is checked against the code as of this writing. Where a piece
is designed but not yet wired end to end, it is marked **in progress** rather
than described as shipped.

## Component map

```mermaid
flowchart TB
    Slack["Slack workspace<br/>(Socket Mode)"]
    GH["GitHub<br/>(push webhook)"]

    subgraph platform["apps/ (Python services)"]
        Dispatcher["apps/dispatcher<br/>Bolt, Socket Mode"]
        Worker["apps/worker<br/>concurrency kernel"]
        API["apps/api<br/>FastAPI"]
    end

    subgraph queue["Valkey"]
        Stream["Stream: agentos:runs<br/>+ dedupe keys, thread locks,<br/>sandbox affinity routes"]
    end

    subgraph substrate["Kubernetes"]
        Sandbox["Agent Sandbox<br/>(claimed, warm pool)"]
        Runner["runner/<br/>claude-agent-sdk streaming server"]
        Sandbox --> Runner
    end

    Postgres[("Postgres<br/>agents / versions / deployments")]
    Store[("MinIO / S3<br/>plugin bundles")]
    Anthropic["Anthropic API"]

    subgraph obs["Observability"]
        Collector["OTel Collector<br/>OTLP gRPC+HTTP in, HTTP out"]
        Langfuse["Langfuse<br/>traces + evals"]
        Collector --> Langfuse
    end

    UI["apps/ui<br/>React console"]
    CLI["cli/<br/>agentos (Rust)"]

    Slack -- "app_mention / message" --> Dispatcher
    Dispatcher -- "XADD QueuedSlackEvent" --> Stream
    Stream -- "XREADGROUP" --> Worker
    Worker -- "claim / resume" --> Sandbox
    Worker -- "POST /v1/event,/steer,/interrupt" --> Runner
    Worker -- "chat.update placeholder" --> Slack
    Runner -- model call --> Anthropic
    Runner -- "OTLP/HTTP spans" --> Collector
    Worker -- "OTLP spans" --> Collector

    GH -- "push (dev/prod branch)" --> API
    API -- "validated bundle" --> Store
    API -- "agents/versions/deployments" --> Postgres
    Runner -. "AGENTOS_PLUGIN_DIR<br/>(in progress: not yet<br/>deployment-resolved)" .-> Store

    UI -- "same-origin /api" --> API
    API -- "trace/metric proxy" --> Langfuse
    API -- "pod log proxy" --> Sandbox
    CLI -- "local Docker runner<br/>(start/send/eval)" --> Runner
    CLI -- "deploy (tar.gz)" --> API
```

**The frozen seam.** `packages/aci-protocol` (the ACI session protocol: inject /
steer / interrupt / stream NDJSON / budget / side-effect flag) is what the
worker, the runner, the CLI, and the UI's trace viewer all compile against, in
three languages (Python source of truth, generated TypeScript, generated Rust).
`packages/plugin-format` (the Claude Code plugin shape verbatim) is what the
bundle pipeline, the CLI's scaffold, and the runner's bundle loader all
compile against. Both are frozen: see [Frozen contracts](#frozen-contracts)
below and the root `CLAUDE.md`.

**Adopted, not built** (ADR-0007): Langfuse (traces + evals), Kubernetes Agent
Sandbox (interactive runtime), Slack Bolt (Socket Mode), Valkey Streams
(queue), Postgres (app state), the OTel Collector. AgentOS builds five things
around that spine: the API, the dispatcher, the worker+runner glue, the UI,
and the CLI.

## Message flow: a Slack mention becomes a threaded reply

```mermaid
sequenceDiagram
    participant U as Slack user
    participant D as Dispatcher (E1)
    participant V as Valkey
    participant W as Worker kernel (F1)
    participant S as Sandbox substrate (G1)
    participant R as Runner (D1)
    participant A as Anthropic API
    participant O as OTel Collector -> Langfuse

    U->>D: app_mention / DM message
    D->>V: SET dedupe:<event_id> NX EX ttl
    Note over D: retried delivery finds the key set,<br/>is dropped (still acked, never re-posted)
    D->>U: post placeholder reply ("On it...")
    D->>V: XADD agentos:runs {QueuedSlackEvent}

    W->>V: XREADGROUP (consumer group)
    W->>V: SET NX PX thread lock (routing decision)
    alt no live turn for this thread
        W->>S: claim(thread_ts)
        S-->>W: SandboxHandle (warm-pool bind, ~0.2s,<br/>cold create on the resume/env path)
        W->>R: POST /v1/event {message}
    else turn already live for this thread
        W->>R: POST /v1/steer {text}
        Note over W,R: 409 if the turn finished first (the finish race),<br/>worker opens a fresh turn on the same idle sandbox
    end
    V-->>W: (lock released once the turn is open,<br/>not held during streaming)

    R->>A: model call (streaming)
    R-->>W: NDJSON: text_delta*, tool_note*, final
    R--)O: gen_ai spans (agent.run -> generation -> tool)
    W->>V: markers (done / side_effect_flag as seen)
    W->>U: chat.update the placeholder in place
    W->>V: XACK
```

Rules the kernel enforces on this loop (each has an integration test in
`apps/worker/tests/kernel/`): one live session per thread, the finish race
(late steer falls back to a fresh turn), steer-vs-interrupt (steer is the
default; interrupt is the explicit hard stop), and no auto-retry after a
side-effectful failure (it escalates to a human instead). See
`apps/worker/CLAUDE.md` for the enforceable version of these rules.

**Suspend/resume is a cold rehydrate, not a live hibernate** (ADR-0003):
suspending a sandbox deletes its pod; resume creates a fresh one and injects
`AGENTOS_HISTORY_REF` so the runner rehydrates from history. Prompt-cache
warmth is real within one continuous claim and is never assumed across a
suspend.

**In progress:** the worker's sandbox claim does not yet resolve which plugin
bundle/version to mount from the deployment the thread belongs to — today's
kernel is the walking-skeleton shape (one fixed runner image/plugin). Wiring
`agentos:runs` events to a specific `deployment_id` and injecting that
deployment's bundle at claim time is part of the remaining Wave 3/4 work
(tracked informally as the SK walking-skeleton gate).

## Deploy flow: a git push becomes a bot identity

```mermaid
sequenceDiagram
    participant Dev as Developer
    participant GH as GitHub
    participant API as apps/api (J1: gitflow.py)
    participant PF as plugin-format validator
    participant S3 as MinIO / S3
    participant PG as Postgres

    Dev->>GH: git push (dev or prod branch)
    GH->>API: POST /github/webhook (push, HMAC-signed)
    API->>API: verify_signature(x-hub-signature-256)
    alt push to dev branch
        API->>API: archive pushed sha (git protocol, no GitHub API needed)
        API->>PF: validate_bundle(archived tree)
        PF-->>API: ValidationResult (errors are path-qualified, actionable)
        API->>S3: store immutable versioned bundle
        API->>PG: create Version + Deployment (environment=dev)
        Note over API: the dev bot identity now serves this sha
    else push to prod branch
        API->>PG: find the already-built Version for this sha
        Note over API: reuses the dev-built bundle,<br/>does not rebuild
        API->>PG: create Deployment (environment=prod)
        Note over API: promotes the same artifact -- no separate prod build
    end
```

`GET /agents`, `/agents/{id}/versions`, `/agents/{id}/versions/{vid}/bundle`
(the CLI's and UI's manual path) and the webhook path both terminate at the
same `Version`/`Deployment` tables in Postgres and the same bundle validator
(`plugin_format.validate_bundle`), so a plugin authored in the browser, pushed
from a CLI `agentos deploy`, or promoted by git-flow all go through one
pipeline. See `apps/api/CLAUDE.md` and `packages/CLAUDE.md`.

## Frozen contracts

Two packages are **frozen interfaces**: every lane compiles against them, so
an unreviewed change in one breaks every other lane silently unless the
schema-compat CI gate catches it.

- **`packages/aci-protocol`** — the ACI session protocol. Pydantic models are
  the source of truth; JSON Schema, generated TypeScript, and generated Rust
  are committed derivatives. `tests/test_schema_compat.py` regenerates them
  in-process and fails on drift.
- **`packages/plugin-format`** — the Claude Code plugin bundle shape,
  verbatim (this is the distribution wedge: compatibility with real Claude
  Code plugins, not an invented format).

A task that needs either package to change **stops and escalates** rather
than working around it — see the root `CLAUDE.md`.

## What is proven vs. what is designed

The infrastructure substrate under this architecture was live-validated on a
real cluster before any of it was built (see `docs/prototype-derisking-review.md`
and the ADRs for the evidence): Agent Sandbox's routable endpoint, warm-pool
sub-second claims, claude-agent-sdk steering/interrupt, prompt-cache reuse
within a claim, Langfuse's trace-tree reconstruction, the security rails
(egress lockdown, secret isolation, non-root/gVisor). What has since been
*built* on top of that proven foundation (per the task DAG in
`docs/build-orchestration-plan.md` and git history): the frozen contracts, the
API server, the runner, the dispatcher, the UI (shell + wired create/deploy/
Runs/Metrics/Logs), the sandbox substrate, the worker kernel, the CLI, the
Helm chart with its security rails (A2), and git-flow (J1).

**Not yet built / explicitly deferred:** the eval runner + PR-check + eval
matrix endpoint (K1), end-to-end budget enforcement wired through the API and
UI Cost view (L1 — the runner already enforces its own per-run token ceiling
and daily USD cap locally), the soak/chaos suite (N1), the walking-skeleton
verification gate (SK) that proves the whole loop live against a real Slack
workspace, the Interview-Me onboarding compiler, and automatic memory
generation. The UI's Fleet, Evals, Versions, Usage, and Cost views still run
on fixture data pending those lanes; see `apps/ui/README.md` for the current
fixture-vs-wired split.
