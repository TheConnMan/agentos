# AgentOS

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
`@agentos`. See [`docs/architecture.md`](docs/architecture.md) for the full
component map, a message-flow sequence diagram, and the deploy-flow sequence
diagram.

## Status

The core spine is built and covered by CI: the frozen contracts, the API
server, the dispatcher, the runner, the sandbox substrate, the worker
concurrency kernel, the UI (shell plus create/deploy/Runs/Metrics/Logs wired
to the real backend), the CLI, the Helm chart with its security rails, and
git-flow (push-to-deploy, merge-to-promote). Still ahead: the eval runner +
PR-check pipeline, end-to-end budget enforcement through the UI's Cost view,
the soak/chaos suite, and the walking-skeleton verification gate against a
real Slack workspace. See "What is proven vs. what is designed" in
[`docs/architecture.md`](docs/architecture.md#what-is-proven-vs-what-is-designed)
for the precise built/in-progress split.

## Component map

| Component | Language | What it owns |
|---|---|---|
| [`apps/dispatcher`](apps/dispatcher/README.md) | Python (Slack Bolt) | Socket Mode ingestion: ack, dedupe, placeholder reply, enqueue |
| [`apps/worker`](apps/worker/README.md) | Python (redis-py) | The concurrency kernel (routing, finish-race, steer/interrupt) + the Agent Sandbox substrate |
| [`runner`](runner/README.md) | Python (claude-agent-sdk) | The streaming session server that implements the ACI contract inside a sandbox |
| [`apps/api`](apps/api/README.md) | Python (FastAPI) | Agents/versions/deployments CRUD, plugin bundle pipeline, GitHub git-flow, Langfuse + pod-log proxies |
| [`apps/ui`](apps/ui/README.md) | React (Vite + TS) | The AgentOS console: author, deploy, and observe agents |
| [`cli`](cli/README.md) | Rust (clap + tokio) | The `agentos` CLI: local emulation, evals, and deploy from a laptop |
| [`charts/agentos`](charts/agentos/README.md) | Helm | The umbrella chart: Langfuse + Postgres + Valkey + ClickHouse + MinIO + OTel Collector + the Agent Sandbox substrate, with security rails on by default |
| [`packages/aci-protocol`](packages/aci-protocol/README.md) | Python (frozen, codegen to TS + Rust) | The ACI session protocol every lane speaks |
| [`packages/plugin-format`](packages/plugin-format/README.md) | Python (frozen, codegen to JSON Schema) | The Claude Code plugin bundle shape, verbatim |

## Three ways to talk to the system

**Real Slack** — connect a workspace, `@mention` the bot in a channel or DM it.
The dispatcher acks, posts a placeholder, and the worker routes the turn into
a claimed sandbox; the placeholder is edited in place as the reply streams.
See `apps/dispatcher/README.md`'s runbook for pointing at a real workspace.

**Local CLI, no Slack at all** — `agentos start` boots the runner image in
Docker (optionally `--fake-model` for a fully offline round-trip), then
`agentos send "..."` emulates a Slack message against it and streams the
NDJSON reply to your terminal. `agentos eval` runs a plugin's `evals/cases.json`
the same way. This is the fastest inner loop for developing a plugin: zero
Slack, zero cluster. See `cli/README.md`.

**`agentos chat` (middle mode)** — drive the real deployed pipeline (worker →
sandboxed runner) without a Slack workspace in between. `agentos deploy` pushes a
bundle to the API, then a worker run with `AGENTOS_SANDBOX_SUBSTRATE=docker`
claims runner containers locally instead of a cluster, and `agentos chat` sends a
message through the same path a Slack mention would take. Middle mode defaults to
a **real model**: export `CLAUDE_CODE_OAUTH_TOKEN` (or `ANTHROPIC_API_KEY`)
before starting the worker and it is forwarded into each runner container. For a
fully offline round-trip, set `AGENTOS_FAKE_MODEL=1` — an explicit test-only knob;
without a credential and without that flag the worker refuses to start rather than
silently faking. See the middle-mode runbook below.

## Quickstart

Everything below runs against the dev stack in `compose.dev.yaml`
(Postgres + Valkey + Langfuse v3 + ClickHouse + MinIO + OTel Collector — see
[`CLAUDE.md`](CLAUDE.md#the-dev-stack) for ports and gotchas).

```bash
# 1. Bring up the backing stack
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
../target/release/agentos start
../target/release/agentos send "hello"
../target/release/agentos eval
```

For a fully offline round-trip (no credential, scripted replies), add
`--fake-model` to `agentos start` — an explicit test-only mode that never reaches
the Anthropic API.

**Middle-mode runbook** (real deployed pipeline, no Slack, no cluster — the
backing stack from the Quickstart plus the API on :8000 and the runner image
built as above):

```bash
# Deploy a plugin to the API (creates the agent bound to a Slack channel id).
./target/release/agentos deploy --plugin-dir ./my-agent --slack-channel C-DEMO --env dev

# Start a worker that claims runner containers via Docker. REAL MODEL is the
# default: export your credential first (forwarded into each runner container).
# AGENTOS_DOCKER_NETWORK joins each runner container to the compose network and
# OTEL_EXPORTER_OTLP_ENDPOINT is forwarded into it; without BOTH, runs still
# execute but emit no traces (the runner cannot resolve otel-collector).
export CLAUDE_CODE_OAUTH_TOKEN=...        # or ANTHROPIC_API_KEY=...
env AGENTOS_SANDBOX_SUBSTRATE=docker \
    VALKEY_HOST=localhost VALKEY_PORT=56379 VALKEY_PASSWORD=valkeypass \
    SLACK_API_BASE_URL=http://localhost:8137/api/ SLACK_BOT_TOKEN=xoxb-dev \
    AGENTOS_DOCKER_NETWORK=agentos_default \
    OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318 \
    uv run python -m agentos_worker &

# Send a message through the same path a Slack mention takes; the fake-model
# transcript is the identical command minus the credential, plus AGENTOS_FAKE_MODEL=1.
./target/release/agentos chat "what changed in the last deploy?" \
    --channel C-DEMO --listen-port 8137
```

For an offline round-trip add `AGENTOS_FAKE_MODEL=1` to the worker env and drop
the credential. Without either, the worker refuses to start.

Prefer a prebuilt binary? Once `v0.1.0` is tagged, releases will attach
`agentos-<target>` binaries (linux + macOS) to the
[GitHub Releases](../../releases) page. No releases are published yet, so
`cargo build` (above) is the way to get the CLI today.

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
- **Concurrent work happens in git worktrees**, one per task/branch, so
  multiple agents (or you and a teammate) can build in parallel without
  clobbering each other's checkout. See `CLAUDE.md` for the exact protocol.
- **Two frozen contracts** (`packages/aci-protocol`, `packages/plugin-format`)
  gate every cross-language lane; changing either requires regenerating the
  committed schema/TS/Rust artifacts and is enforced by a CI compat test.

## Where to go next

- [`docs/architecture.md`](docs/architecture.md) — the component diagram, the
  message-flow and deploy-flow sequence diagrams, and the built/in-progress
  split.
- [`docs/adr/`](docs/adr/) — the load-bearing architecture decisions (Agent
  Sandbox as substrate, stateless-first sessions, Langfuse as the
  observability backbone, the frozen ACI, security rails as chart defaults,
  adopt-not-build boundaries), each with the live-cluster evidence behind it.
- [`docs/mvp-build-plan.md`](docs/mvp-build-plan.md) and
  [`docs/build-orchestration-plan.md`](docs/build-orchestration-plan.md) —
  historical planning artifacts: the architecture spine and the task DAG this
  repo was built along. Useful for understanding sequencing decisions; not
  living documentation.
- [`CLAUDE.md`](CLAUDE.md) — the operative rules for anyone (human or agent)
  working in this repo: the worktree protocol, verify commands, the dev
  stack, and the frozen-contract escalation rule. Each top-level directory
  also has its own scoped `CLAUDE.md` with rules specific to that area.
