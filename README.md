# AgentOS

[![CI](https://github.com/curie-eng/agentos/actions/workflows/ci.yaml/badge.svg)](https://github.com/curie-eng/agentos/actions/workflows/ci.yaml)

Open-source, self-hostable developer platform for Slack-based agents. Connect
Slack, author a Claude-Code-format plugin (skills + tools + MCP), deploy it as
a versioned bot identity, and get traces, evals, budgets, and git-flow for
free.

"Relay" is this project's internal codename (repo, commits, internal docs);
"agentos" is the product-surface name — the CLI binary, the bot handle, the
console. Both names refer to the same system.

## What it does

A Slack `@mention` (or DM) is answered by a versioned plugin running in an
isolated Kubernetes sandbox, with the run traced end to end and steerable
mid-turn. A `git push` deploys that plugin under a bot identity: push to `dev`
updates `@agentos-dev`, merging to `prod` promotes the same built artifact to
`@agentos`. See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full
component map, a message-flow sequence diagram, and the deploy-flow sequence
diagram.

## Status

The core spine is built, covered by CI, and was live-verified end to end
against a real Slack workspace on a real model: the frozen contracts, the API
server, the dispatcher, the runner, the sandbox substrate (Kubernetes and
local Docker), the worker concurrency kernel with deployment-to-runtime
binding, the UI (shell plus create/deploy/Runs/Metrics/Logs/Cost/Versions
wired to the real backend), the CLI (local inner loop plus the cluster
operator lifecycle), the Helm chart with its security rails and the
runner-image prewarm DaemonSet, git-flow (push-to-deploy, merge-to-promote),
the eval plane (eval-stream consumer, matrix endpoint, PR-check reporter), and
budgets plus the kill switch. Still ahead: wiring the UI's Evals matrix and
Usage/Settings views to their existing backends, the soak/chaos suite, and
retiring the fixture/showroom surface. See "What is built vs deferred" in
[`ARCHITECTURE.md`](ARCHITECTURE.md#11-what-is-built-vs-deferred)
for the precise built/in-progress split.

Forward-looking work is planned and tracked in
[GitHub issues](https://github.com/curie-eng/agentos/issues), with larger
journeys filed as `epic`-labeled issues.

## Component map

| Component | Language | What it owns |
|---|---|---|
| [`apps/dispatcher`](apps/dispatcher/README.md) | Python (Slack Bolt) | Socket Mode ingestion: ack, dedupe, placeholder reply, enqueue |
| [`apps/worker`](apps/worker/README.md) | Python (redis-py) | The concurrency kernel (routing, finish-race, steer/interrupt) + the Agent Sandbox substrate |
| [`runner`](runner/README.md) | Python (claude-agent-sdk) | The streaming session server that implements the ACI contract inside a sandbox |
| [`apps/api`](apps/api/README.md) | Python (FastAPI) | Agents/versions/deployments CRUD, plugin bundle pipeline, GitHub git-flow, Langfuse + pod-log proxies |
| [`apps/ui`](apps/ui/README.md) | React (Vite + TS) | The AgentOS console: author, deploy, and observe agents |
| [`cli`](cli/README.md) | Rust (clap + tokio) | The `agentos` CLI: local emulation, evals, deploy, and the cluster operator lifecycle (`up`/`status`/`down`) |
| [`charts/agentos`](charts/agentos/README.md) | Helm | The umbrella chart: Langfuse + Postgres + Valkey + ClickHouse + MinIO + OTel Collector + the Agent Sandbox substrate, with security rails on by default |
| [`packages/aci-protocol`](packages/aci-protocol/README.md) | Python (frozen, codegen to TS + Rust) | The ACI session protocol every lane speaks |
| [`packages/plugin-format`](packages/plugin-format/README.md) | Python (frozen, codegen to JSON Schema) | The Claude Code plugin bundle shape, verbatim |

## Which target do I want?

Every CLI command that touches an environment takes a **target noun** in the
middle: `skill`, `local`, or `cluster`. Pick the lightest one that answers your
question. (`agentos init` is the exception: it scaffolds a bundle on disk and
targets no environment.)

| Target | What runs | Slack | Kubernetes | Verbs | Reach for it to |
|---|---|---|---|---|---|
| `skill` | Just the runner container on the host Docker daemon. No platform, no queue, no API, no Slack. Fully offline. | none | none | `up` `down` `status` `message` `eval` | Iterate a plugin/skill against a local runner, the fastest loop. |
| `local` | The full platform via docker compose (Postgres + Valkey + Langfuse + API + worker). | none | none | `up` `down` `status` `message` `deploy` | Exercise the real queue -> worker -> sandbox -> reply product loop with zero Slack and zero Kubernetes. Its API is published on host port `28000`. |
| `cluster` | The platform on Kubernetes (a Helm release). | optional | yes | `up` `down` `status` `message` `deploy` | Operate and drive a deployed cluster release. |

The universal quartet `up`/`down`/`status`/`message` is on all three targets;
`skill` adds `eval`, and `local`/`cluster` add `deploy`.

The distinction that matters: `skill` is the **runner-only** loop, it boots just
the runner container and talks straight to its ACI HTTP surface with no platform
in front. `local` and `cluster` put the **full platform** (queue, worker,
sandbox) in front of the identical runner and ACI, so a `message` walks the same
path a real Slack mention would take.

**`skill` (runner-only, fully offline).** `agentos skill up` boots the runner
image in Docker (add `--fake-model` for a fully offline round-trip), then
`agentos skill message "..."` sends a synthetic Slack event to it and streams
the NDJSON reply to your terminal. `agentos skill eval` runs a plugin's
`evals/cases.json` the same way. Abort a live `skill message` with Ctrl-C. This
is the fastest inner loop for developing a plugin: zero Slack, zero platform,
zero cluster. See `cli/README.md`.

**`local` (full platform via compose, no Slack).** `agentos local up` brings up
the compose stack, `agentos local deploy` pushes a bundle to the compose API,
and `agentos local message "..."` drives a message through the real
queue -> worker -> sandboxed runner -> reply path with no Slack and no
Kubernetes. The compose worker runs the fake model by default; for a real model,
export `CLAUDE_CODE_OAUTH_TOKEN` (or `ANTHROPIC_API_KEY`) and set
`AGENTOS_FAKE_MODEL=0` in the compose environment. See the local runbook below.

**`cluster` (deployed Helm release).** `agentos cluster up` installs the platform
on Kubernetes and `agentos cluster message "..."` drives the deployed release end
to end with no Slack. See
[Operating a cluster install](#operating-a-cluster-install) and
[`docs/operations.md`](docs/operations.md).

**Real Slack (production).** With a workspace connected, `@mention` the bot in a
channel or DM it: the dispatcher acks, posts a placeholder, and the worker routes
the turn into a claimed sandbox; the placeholder is edited in place as the reply
streams. This is the same platform the `local` and `cluster` targets exercise,
with Slack in front. See `apps/dispatcher/README.md`'s runbook for pointing at a
real workspace.

## Prerequisites

- **[uv](https://docs.astral.sh/uv/)**: the Python workspace manager.
- **Python 3.13** (the workspace requires `>=3.13`).
- **Docker + Compose v2**: for the dev stack and the local runner container.
- **Node.js 22 + [pnpm](https://pnpm.io/)**: for the UI.
- **Rust toolchain** (stable, edition 2021, with `rustfmt` and `clippy`): for
  the CLI. Skip if you use a prebuilt CLI binary from Releases.
- **kubectl + helm**: only for the cluster-install path (see
  [Operating a cluster install](#operating-a-cluster-install)).

## Quickstart

Everything below runs against the dev stack in `compose.dev.yaml`
(Postgres + Valkey + Langfuse v3 + ClickHouse + MinIO + OTel Collector). See
[`AGENTS.md`](AGENTS.md) for the ports each service binds and the load-bearing
gotchas.

```bash
# 1. Bring up the backing stack. The stack runs on baked defaults; copy
#    .env.example to the gitignored .env only if you need to override anything.
cp .env.example .env    # optional
docker compose -f compose.dev.yaml up -d
docker compose -f compose.dev.yaml ps    # wait for all services healthy

# 2. Install the Python workspace (uv workspace: aci-protocol, plugin-format,
#    apps/api, apps/dispatcher, apps/worker, runner)
uv sync

# 3. Run the test suite
uv run pytest -q
uv run ruff check .
uv run mypy

# 4. Boot the API server (needs the Postgres schema applied once)
cd apps/api && uv run alembic upgrade head
uv run uvicorn agentos_api.main:app --port 8000 &
cd -

# 5. Boot the UI (fixture mode by default; ?api=1 wires it to the running API)
cd apps/ui && pnpm install && pnpm dev
# open http://localhost:5173/?state=1        (fixture demo)
# open http://localhost:5173/?api=1&state=1  (wired to apps/api on :8000)
```

**CLI walkthrough** (no Slack, no cluster — needs the runner image built once:
`docker build -f runner/Dockerfile -t agentos-runner .` from the repo root):

```bash
cd cli && cargo build --release
./target/release/agentos init my-agent && cd my-agent
# Real model is the default. Export a credential first (forwarded into the runner
# container): CLAUDE_CODE_OAUTH_TOKEN, ANTHROPIC_API_KEY, or AGENTOS_CREDENTIALS.
export CLAUDE_CODE_OAUTH_TOKEN=...
../target/release/agentos skill up
../target/release/agentos skill message "hello"
../target/release/agentos skill eval
```

A committed first-party example lives at `examples/weather/`. `cd examples/weather && agentos start` runs it from a clean clone. `agentos init` scaffolds this same weather template, so every fresh bundle starts as a runnable web-search skill to learn from and edit.

For a fully offline round-trip (no credential, scripted replies), add
`--fake-model` to `agentos skill up` — an explicit test-only mode that never reaches
the Anthropic API.

**One-command middle mode** (the fastest path — no host-run worker, no cluster):

```bash
agentos local up   # brings up the backing stores + API + a containerized worker + the console UI
agentos local deploy --plugin-dir ./my-agent --slack-channel C-DEMO --api-url http://localhost:28000
agentos local message "what changed in the last deploy?"
```

`local up` runs the worker as the `agentos-worker` compose service (fake model by
default), so there is nothing to hand-run. For a real model, export a credential
and set `AGENTOS_FAKE_MODEL=0` in the compose environment. The manual runbook
below is the equivalent with a host-process worker, useful when iterating on the
worker itself from source. The console UI is served at `http://localhost:28080/?api=1`; open it to reach the console wired to your local compose API.

`agentos local up` publishes the API on `:28000` (the compose host port); the
hand-run `uvicorn` in Quickstart step 4 uses `:8000`. Point `deploy --api-url`
at whichever one you brought up.

**Local runbook** (real deployed pipeline, no Slack, no cluster — the
backing stack from the Quickstart plus the API on :8000 and the runner image
built as above):

```bash
# Deploy a plugin to the API (creates the agent bound to a Slack channel id).
# This runbook hand-starts uvicorn on :8000 (Quickstart step 4), so pin
# --api-url to it; local deploy otherwise defaults to the `local up` API on :28000.
./target/release/agentos local deploy --plugin-dir ./my-agent --slack-channel C-DEMO \
    --api-url http://localhost:8000 --env dev

# Start a worker that claims runner containers via Docker. REAL MODEL is the
# default: export your credential first (forwarded into each runner container).
# AGENTOS_DOCKER_NETWORK joins each runner container to the compose network and
# OTEL_EXPORTER_OTLP_ENDPOINT is forwarded into it; without BOTH, runs still
# execute but emit no traces (the runner cannot resolve otel-collector).
export CLAUDE_CODE_OAUTH_TOKEN=...        # or ANTHROPIC_API_KEY=...
env AGENTOS_SANDBOX_SUBSTRATE=docker \
    VALKEY_HOST=localhost VALKEY_PORT=26379 VALKEY_PASSWORD=valkeypass \
    SLACK_API_BASE_URL=http://localhost:8155/api/ SLACK_BOT_TOKEN=xoxb-dev \
    AGENTOS_DOCKER_NETWORK=agentos_default \
    OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318 \
    uv run python -m agentos_worker &

# Send a message through the same path a Slack mention takes; the fake-model
# transcript is the identical command minus the credential, plus AGENTOS_FAKE_MODEL=1.
./target/release/agentos local message "what changed in the last deploy?" \
    --channel C-DEMO
```

For an offline round-trip add `AGENTOS_FAKE_MODEL=1` to the worker env and drop
the credential. Without either, the worker refuses to start.

Prefer a prebuilt binary? The
[GitHub Releases](https://github.com/curie-eng/agentos/releases) page attaches
`agentos-<target>` binaries on every tag push (`agentos-x86_64-unknown-linux-gnu`
and `agentos-aarch64-apple-darwin`), so `v0.1.0` onward you can download the CLI
instead of running `cargo build` (above).

Each package documents its own deeper verify commands and gotchas in its own
README (linked above) and its own scoped `CLAUDE.md` — this quickstart is
enough to see the pieces move; it is not a substitute for those.

## Dev workflow

- **Python packages** are one `uv` workspace (root `pyproject.toml`); ruff,
  mypy, and pytest run across all members from the repo root (see step 3
  above). Integration tests hit the real dev stack, never mocks of
  Postgres/Valkey/Langfuse.
- **The Rust CLI** and **the UI** are verified independently; see
  `cli/README.md` and `apps/ui/README.md` for their commands.
- **Work on a feature branch per change**, cut from `main`; never commit to
  `main` directly.
- **Two frozen contracts** (`packages/aci-protocol`, `packages/plugin-format`)
  gate every cross-language lane; changing either requires regenerating the
  committed schema/TS/Rust artifacts and is enforced by a CI compat test.

## Operating a cluster install

The same `agentos` binary installs and runs the platform on a Kubernetes
cluster, wrapping the umbrella Helm chart the way `linkerd` or `cilium` wrap
theirs. The short version:

- `agentos cluster up` runs `helm upgrade --install` of `charts/agentos`; it reads
  `AGENTOS_MODEL_CREDENTIALS` to enable a real model (absent, the release
  installs sealed with canned replies), and `--no-expose` keeps the UI and
  Langfuse ClusterIP-only. Connecting Slack is a raw `helm upgrade
  --reuse-values` (not a CLI verb; the chart's `NOTES.txt` prints it).
- `agentos cluster status` reports release health and access URLs; `agentos cluster down`
  uninstalls and sweeps the runtime namespaces. Every verb takes `--dry-run`.
- `agentos cluster message "..."` drives a deployed release end to end with no Slack.

Full runbook (the credential model, the Slack-connect command, and the
zero-Slack `message` flow) is in [`docs/operations.md`](docs/operations.md).

## Where to go next

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — the component diagram, the
  message-flow and deploy-flow sequence diagrams, and the built/in-progress
  split.
- [`docs/adr/`](docs/adr/) — the load-bearing architecture decisions (Agent
  Sandbox as substrate, stateless-first sessions, Langfuse as the
  observability backbone, the frozen ACI, security rails as chart defaults,
  adopt-not-build boundaries), each with the live-cluster evidence behind it.
- [`AGENTS.md`](AGENTS.md) — the operative rules for anyone (human or agent)
  working in this repo: the verify commands, the dev stack, the
  frozen-contract escalation rule, and the build gotchas. Each top-level
  directory also has its own scoped `CLAUDE.md` with rules specific to that area.
