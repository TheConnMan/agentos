# AgentOS

[![CI](https://github.com/curie-eng/agentos/actions/workflows/ci.yaml/badge.svg)](https://github.com/curie-eng/agentos/actions/workflows/ci.yaml)

Open-source, self-hostable developer platform for Slack-based agents. Connect
Slack, author a Claude-Code-format plugin (skills + tools + MCP), deploy it as
a versioned bot identity, and get traces, evals, budgets, and git-flow for
free.

New here? Start with [`QUICKSTART.md`](QUICKSTART.md) to get your first agent
reply in about a minute. Then read [`docs/vision.md`](docs/vision.md) for what
AgentOS is, who it is for, and what it could become. It is the north star we
hold new features against.

"Relay" is this project's internal codename (repo, commits, internal docs);
"agentos" is the product-surface name — the CLI binary, the bot handle, the
console. Both names refer to the same system.

## What does AgentOS do?

Your agent worked on your laptop and then broke once it was deployed. AgentOS is
a harness that runs the **same immutable bundle** and the **same
`evals/cases.json`** identically across three targets (`skill` in-process,
`local` via docker compose, `cluster` on Kubernetes), so a tier-to-tier
divergence surfaces as the harness catching a real environment bug before
production, not a silent regression after it. A coding agent can already write a
`skill.md`; what it cannot guarantee alone is that the skill behaves the same
deployed as it did locally. That guarantee is the harness's job: the local loop
is the production loop.

A Slack `@mention` (or DM) is answered by a versioned plugin running in an
isolated Kubernetes sandbox, with the run traced end to end and steerable
mid-turn. A `git push` deploys that plugin under a bot identity: push to `dev`
updates `@agentos-dev`, merging to `prod` promotes the same built artifact to
`@agentos`. See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full
component map, a message-flow sequence diagram, and the deploy-flow sequence
diagram.

## Why did my agent work locally but break once deployed?

Because "locally" and "deployed" were different runtimes: a different Python, a
missing tool, an MCP server that resolved on your laptop and not in the cluster,
a credential that was present in one place and absent in the other. AgentOS
removes that gap by construction. The thing you run locally and the thing that
runs in production are the **same immutable bundle** claimed by the **same
runner image** speaking the **same frozen ACI contract**; only the substrate
underneath changes (in-process, docker compose, Kubernetes). So a difference in
behavior between tiers is a real environment difference the harness surfaces —
not a mystery you debug after users hit it. Start from the
[target table](#which-target-do-i-want) and climb `skill` → `local` → `cluster`;
each rung is the same bundle on a heavier substrate.

## How do I test an agent the same way locally and on Kubernetes?

You run the **same `evals/cases.json`** at every tier. `agentos skill eval`
grades the bundle in-process; the `local` and `cluster` targets drive the same
cases through the real queue → worker → sandbox → runner path a Slack mention
takes, so an eval that passes on your laptop and fails on the cluster is the
harness catching a deployment bug, not a flaky test. The grader, the case shape,
and the bundle are identical across tiers — that identity is the whole point (a
tier-to-tier eval divergence is signal, not noise). See
[How do I develop and verify a change?](#how-do-i-develop-and-verify-a-change)
for the per-package verify commands and
[`cli/README.md`](cli/README.md) for the full `eval` reference across targets.

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
targets no environment.) The point of the three targets is that the identical
bundle and the identical `evals/cases.json` run across all of them, so promoting
`skill` to `local` to `cluster` is a parity ladder, not three separate setups.

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
the `full` compose profile by default. `agentos local up --minimal` brings up
the smaller `core` profile (API, worker, Postgres, Valkey, MinIO). Then
`agentos local deploy` pushes a bundle to the compose API, and
`agentos local message "..."` drives a message through the real
queue -> worker -> sandboxed runner -> reply path with no Slack and no
Kubernetes. The compose worker runs the fake model by default, but exporting
`CLAUDE_CODE_OAUTH_TOKEN` (or `ANTHROPIC_API_KEY`) in your shell is enough for a
real model -- `agentos local up` flips to live automatically when a credential is
present, matching `skill up`, so there is no manual `AGENTOS_FAKE_MODEL=0` step.
See the local runbook below.

**`cluster` (deployed Helm release).** `agentos cluster up` installs the platform
on Kubernetes and `agentos cluster message "..."` drives the deployed release end
to end with no Slack. See
[How do I run my agent on a Kubernetes cluster?](#how-do-i-run-my-agent-on-a-kubernetes-cluster)
and [`docs/operations.md`](docs/operations.md).

**Real Slack (production).** With a workspace connected, `@mention` the bot in a
channel or DM it: the dispatcher acks, posts a placeholder, and the worker routes
the turn into a claimed sandbox; the placeholder is edited in place as the reply
streams. This is the same platform the `local` and `cluster` targets exercise,
with Slack in front. See `apps/dispatcher/README.md`'s runbook for pointing at a
real workspace.

**Connect a real Slack workspace (local).** The `local` target uses the Slack
stub by default, but you can exercise real Slack routing, mentions and threads
on the compose stack without a cluster. Export the two Slack tokens, empty
`SLACK_API_BASE_URL` to un-wire the worker's Slack stub, and start the optional
dispatcher:

```bash
export SLACK_APP_TOKEN=xapp-... SLACK_BOT_TOKEN=xoxb-...
export SLACK_API_BASE_URL=          # empty un-wires the worker's Slack stub
agentos local up --slack

# Raw Docker needs the base profile plus Slack (the dispatcher depends on
# valkey, a core service), for either compose file:
docker compose --profile full --profile slack -f compose.dev.yaml up -d
docker compose --profile full --profile slack -f compose.release.yaml up -d
```

Slack allows exactly one Socket Mode owner per app token at a time, so do not
also run a cluster dispatcher on the same Slack app: it is either/or per app.
Because these are shell exports they persist for the session, so a later plain
`agentos local up` in the same shell keeps the worker pointed at real Slack
(empty `SLACK_API_BASE_URL`) with the real bot token but no dispatcher feeding
the queue; open a fresh shell (or `unset SLACK_API_BASE_URL`) to return to the
Slack-free stub. For the full walkthrough (app creation from the manifest,
channel binding, and troubleshooting) see
[`docs/slack-local-runbook.md`](docs/slack-local-runbook.md).

## Prerequisites

- **[uv](https://docs.astral.sh/uv/)**: the Python workspace manager.
- **Python 3.13** (the workspace requires `>=3.13`).
- **Docker + Compose v2**: for the dev stack and the local runner container.
- **Node.js 22 + [pnpm](https://pnpm.io/)**: for the UI.
- **Rust toolchain** (stable, edition 2021, with `rustfmt` and `clippy`): only
  for building the CLI from source. Skip it entirely if you download the
  prebuilt CLI binary from Releases (the default install path below).
- **kubectl + helm**: only for the cluster-install path (see
  [How do I run my agent on a Kubernetes cluster?](#how-do-i-run-my-agent-on-a-kubernetes-cluster)).

## Quickstart

The fastest way in — for operators and coding agents alike — is the prebuilt
release binary. It needs no Rust toolchain and no repo checkout:

```bash
# Linux (x86_64); for macOS Apple silicon swap in agentos-aarch64-apple-darwin
curl -L -o agentos \
  https://github.com/curie-eng/agentos/releases/latest/download/agentos-x86_64-unknown-linux-gnu
chmod +x agentos && sudo mv agentos /usr/local/bin/
agentos cluster up
agentos local up
```

A release binary needs no repo checkout for `agentos cluster up` or `agentos local up`.
It pulls the pinned chart release asset and the pinned `compose.release.yaml`
matching the binary version, then caches them under `~/.cache/agentos/`. For
local development overrides, pass `-f <compose>`, `--chart <path>`, or
`--image <ref>`.

Contributors can still run from a repo checkout against the dev stack in
`compose.dev.yaml` (Postgres + Valkey + Langfuse v3 + ClickHouse + MinIO + OTel
Collector). See [`AGENTS.md`](AGENTS.md) for the ports each service binds and
the load-bearing gotchas.

```bash
# 1. Bring up the backing stack. The stack runs on baked defaults; copy
#    .env.example to the gitignored .env only if you need to override anything.
cp .env.example .env    # optional
docker compose --profile full -f compose.dev.yaml up -d
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

**CLI walkthrough** (no Slack, no cluster). With the prebuilt `agentos` on
your PATH (from the Quickstart above), the binary pulls the pinned runner
image from GHCR on first run — nothing to build:

```bash
agentos init my-agent && cd my-agent
# Real model is the default. Export a credential first (forwarded into the runner
# container): CLAUDE_CODE_OAUTH_TOKEN, ANTHROPIC_API_KEY, or AGENTOS_CREDENTIALS.
export CLAUDE_CODE_OAUTH_TOKEN=...
agentos skill up
agentos skill message "hello"
agentos skill eval
```

From a source checkout instead (contributor path): `cd cli && cargo build
--release`, build the runner image once with `agentos build`, then run
`./target/release/agentos` in place of `agentos`.

A committed first-party example lives at `examples/weather/`. `cd examples/weather && agentos skill up` runs it from a clean clone. `agentos init` scaffolds a generic starter skill named for your agent (plus a root `AGENTS.md` and an installable `.claude/skills/using-agentos/SKILL.md` harness primer) that you edit to build your own agent; see `examples/weather/` for a runnable example to learn from.

For the "engine as an in-bundle stdio MCP server" shape — a bundle that ships
its own tools as a stdio subprocess the harness spawns, which a hosted harness
cannot do — see the template at [`examples/text-stats-engine/`](examples/text-stats-engine/README.md).

For a fully offline round-trip (no credential, scripted replies), add
`--fake-model` to `agentos skill up` — an explicit test-only mode that never reaches
the Anthropic API.

**One-command middle mode** (the fastest path — no host-run worker, no cluster):

```bash
agentos local up   # full profile: stores + API + worker + Langfuse + UI
# or: agentos local up --minimal   # core profile: stores + API + worker only
agentos local deploy --plugin-dir ./my-agent --slack-channel C-DEMO --api-url http://localhost:28000
agentos local message "what changed in the last deploy?"
```

`local up` runs the worker as the `agentos-worker` compose service (fake model by
default), so there is nothing to hand-run. For a real model, export a credential
in your shell and `local up` goes live automatically. The manual runbook
below is the equivalent with a host-process worker, useful when iterating on the
worker itself from source. The console UI is served at
`http://localhost:28080/?api=1` when you use the default `full` profile.

**Local-model demo mode** is an opt-in offline path that runs a real local model
through an Anthropic-compatible endpoint, so the demo answers for real and can
drive a 1-2 tool-call loop with no Anthropic key. This is a DEMO / dev-loop path,
NOT the production agent path. The fake model stays the zero-dependency default.

Use one flag on the CLI surface you are running:

```bash
agentos skill up --local-model
agentos local up --local-model
agentos cluster up --local-model
```

Bare `--local-model` uses `qwen3:4b`. Override it by passing a model name:

```bash
agentos local up --local-model qwen3-coder:30b
```

Combine `--minimal` with `--local-model` when you want the core local loop plus
Ollama, without Langfuse or the UI:

```bash
agentos local up --minimal --local-model
```

`skill up` and `local up` run the model in a Docker container and point spawned
runners at that endpoint. Both the `skill up --local-model` and compose paths
persist the pulled model in a Docker volume, so a re-up is fast and does not
re-download the model; the skill-path volume is named `<container>-ollama-data`
(the compose path uses `ollama_data`) and can be reclaimed with
`docker volume rm <volume>`. `cluster up` uses the in-chart inference Deployment;
the chart renders the Ollama Service and Deployment, opens the runner egress
carve-out automatically, and bakes `ANTHROPIC_BASE_URL` plus the inference model
into the runner template.

| Model | Loaded (Q4) | Min box | Notes |
|---|---|---|---|
| qwen3:4b | ~2.5GB | 8GB | demo default; clears the 1-2 tool-call bar |
| qwen3-coder:30b | ~17-19GB | 32GB | MoE 30B/3.3B-active; real agentic-coding upgrade |
| gemma4:e4b | ~5GB | 16GB | "4.5B effective" name understates RAM; needs Ollama >=0.31.x |

Gotchas: Ollama 0.24.0 fails `gemma4` with `unknown model architecture`; qwen3
works on 0.24.0 and gemma4 needs >=0.31.x. Gemma HF repos are gated and return
HTTP 400 on `hf.co/google/...`; use a non-gated mirror such as
`hf.co/unsloth/gemma-4-E4B-it-GGUF:<quant>`. RAM sizing tracks the loaded
footprint, not the "effective params" marketing number.

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

Prefer a prebuilt binary for local commands? The
[GitHub Releases](https://github.com/curie-eng/agentos/releases) page attaches
`agentos-<target>` binaries on every tag push (`agentos-x86_64-unknown-linux-gnu`
and `agentos-aarch64-apple-darwin`), so `v0.1.0` onward you can download the CLI
instead of running `cargo build` above.

Each package documents its own deeper verify commands and gotchas in its own
README (linked above) and its own scoped `CLAUDE.md` — this quickstart is
enough to see the pieces move; it is not a substitute for those.

## How do I develop and verify a change?

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

## How do I run my agent on a Kubernetes cluster?

The same `agentos` binary installs and runs the platform on a Kubernetes
cluster, wrapping the umbrella Helm chart the way `linkerd` or `cilium` wrap
theirs. A downloaded release binary resolves the pinned chart release asset for
its version, and `agentos local up` likewise resolves the pinned
`compose.release.yaml`, caching both under `~/.cache/agentos/` with no repo
checkout needed.
Use `--chart <path>` when developing the chart locally. The short version:

- `agentos cluster up` runs `helm upgrade --install` of `charts/agentos`; it reads
  `AGENTOS_MODEL_CREDENTIALS` to enable a real model (absent, the release
  installs sealed with canned replies), and `--no-expose` keeps the UI and
  Langfuse ClusterIP-only. The credential alone still leaves the runner sandbox
  sealed against default-deny egress, so a real model stays unreachable until you
  open its provider with `--allow-egress-host <provider>` (currently `anthropic`
  or `openrouter`). `--allow-web-egress <CIDR>` (repeatable) opens runner egress
  for skill web access (e.g. the weather example's live web search), additive to
  the sealed default; `--allow-egress-host` is the model-provider convenience on
  top of it. Connecting Slack is a raw `helm upgrade
  --reuse-values` (not a CLI verb; the chart's `NOTES.txt` prints it).
- `agentos cluster status` reports release health and access URLs; `agentos cluster down`
  uninstalls and sweeps the runtime namespaces. Every verb takes `--dry-run`.
- `agentos cluster message "..."` drives a deployed release end to end with no Slack.

Full runbook (the credential model, the Slack-connect command, and the
zero-Slack `message` flow) is in [`docs/operations.md`](docs/operations.md).

## Where do I go next?

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
