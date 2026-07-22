# AgentOS quickstart

Welcome. This gets you from nothing to your first agent reply in about a minute,
with no credentials, no cluster, and no Slack. It runs the `skill` target: just
the runner container on your host Docker daemon, talking straight to the agent.
Past that, this is also the detailed, advanced-usage doc: a real model, an
offline local-model demo, the full local/cluster runbooks, and the contributor
repo-checkout path. For the 30-second version, see the
[README Quickstart](README.md#quickstart).

## Before you start

- **Docker** running locally.
- The **`agentos`** binary on your PATH. Download, verify, and install the
  prebuilt binary for your platform by following
  [`docs/release-verification.md`](docs/release-verification.md#verify-the-cli-before-installing-it).

  Building the CLI from source (`cargo build --release` in `cli/`) is the
  contributor path — see [`docs/onboarding.md`](docs/onboarding.md) and
  [Contributor path](#contributor-path-repo-checkout) below.

## Your first agent reply

```bash
# 1. Scaffold a bundle. This creates a starter skill named for your agent, plus
#    an AGENTS.md and a using-agentos harness-primer skill, that you edit next.
agentos init my-agent
cd my-agent

# 2. Boot the runner with the built-in fake model (offline, instant, no key).
agentos skill up --fake-model

# 3. Ask it something and watch the reply stream back.
agentos skill message "hello, are you there?"

# 4. Done. Tear the runner down.
agentos skill down
```

That is the full loop. `agentos skill up` starts the runner container,
`agentos skill message` sends a synthetic event and streams the reply, and
`agentos skill down` stops it. Edit `skills/my-agent/SKILL.md` in the bundle
and re-run steps 2 and 3 to see your change answer.

A committed first-party example lives at `examples/weather/`: `cd
examples/weather && agentos skill up` runs it from a clean clone. For the
"engine as an in-bundle stdio MCP server" shape — a bundle that ships its own
tools as a stdio subprocess the harness spawns — see the template at
[`examples/text-stats-engine/`](examples/text-stats-engine/README.md).

## Level up: a real model

The fake model returns scripted replies. For a genuine answer, drop
`--fake-model` and export a credential first (`agentos` forwards it into the
runner container), then re-run `skill up`. Any one of `CLAUDE_CODE_OAUTH_TOKEN`,
`ANTHROPIC_API_KEY`, or `AGENTOS_CREDENTIALS` works for the Anthropic default:

```bash
export CLAUDE_CODE_OAUTH_TOKEN=...        # or ANTHROPIC_API_KEY=... / AGENTOS_CREDENTIALS=...
agentos skill up
agentos skill message "What's the weather in Paris? Answer in one short sentence."
agentos skill down
```

To use a different provider or model instead of the Anthropic default, bring
your own model through OpenRouter on the same `skill` path. Set
`AGENTOS_CREDENTIALS` to your OpenRouter key (it must arrive on that variable,
not `ANTHROPIC_API_KEY`, and here it selects OpenRouter specifically because
it's paired with `--image`/`--model`) and name a model slug:

```bash
AGENTOS_CREDENTIALS="$OPENROUTER_TOKEN" agentos skill up \
  --image ghcr.io/curie-eng/agentos-runner:latest \
  --model z-ai/glm-5.2

agentos skill message "What's the weather in Paris? Answer in one short sentence."
agentos skill down
```

You should get a real answer instead of the canned loop.

## Offline demo: a real local model, no Anthropic key

`--local-model` is an opt-in offline path that runs a real local model through
an Anthropic-compatible endpoint, so the demo answers for real and can drive a
1-2 tool-call loop with no Anthropic key. This is a DEMO / dev-loop path, NOT
the production agent path — the fake model stays the zero-dependency default.

Use the flag on the CLI surface you are running:

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

## Still have time? Run your skill on your local box or on a Kubernetes cluster

The `skill` loop is just the runner container. To see your skill run through the
real product path (queue, worker, sandbox, traces), the same way a Slack mention
would, put the full platform in front of it. Two options, lightest first.

### On your local box (Docker stack)

`local` brings up the platform with docker compose and runs your skill through
it. `agentos local up` uses the `full` profile, which includes Langfuse and the
console UI. `agentos local up --minimal` boots only the 7 core services for low
RAM machines and the CLI turn path. Same runner, now with the queue and worker
in front, all on your machine. Run these from your bundle directory:

```bash
agentos local up                      # full profile
# or: agentos local up --minimal      # core profile
agentos local deploy --plugin-dir .   # push the bundle to the local API on :28000
agentos local message "What's the weather in Paris?"
agentos local down                    # tear it all down
```

Watch the run land in the console UI at `http://localhost:28080/?api=1`. Like
`skill`, the local stack runs the fake model by default, so replies are scripted
until you wire a real model (`export CLAUDE_CODE_OAUTH_TOKEN=...` or
`ANTHROPIC_API_KEY=...` before `local up`, which flips to live automatically
when a credential is present).

> The stack comes up clean; the UI answers on `:28080`.

For the deeper ticket-verification runbook — rebuilding and redeploying a
changed bundle, watching worker/runner logs, and driving multi-turn
conversations — see [`docs/onboarding.md`](docs/onboarding.md). To hand-run the
worker as a bare host process instead of a compose service (e.g. to attach a
debugger while iterating on worker source), see
[`apps/worker/README.md`](apps/worker/README.md#running-the-worker-as-a-bare-process-agentos_workerrun-docker-substrate).

### On a Kubernetes cluster (Helm)

`cluster` installs the platform as a Helm release and drives your skill on real
Kubernetes, with runs traced in Langfuse. The short arc:

```bash
agentos cluster up                    # install the platform (Helm release)
agentos cluster status                # prints the UI URL, e.g. http://<node>:30080
# The UI reverse-proxies /api to the in-cluster API, so deploy through it -- no
# port-forward. Use the UI URL from `cluster status` with a /api suffix:
agentos cluster deploy --plugin-dir . --api-url http://<node>:30080/api
agentos cluster message "What's the weather in Paris?"
agentos cluster down --yes            # uninstall
```

For a real model on the cluster, the runner model key is
`--set agentSandbox.runner.model=<slug>` (not `runner.model`, which silently
no-ops). The full cluster runbook, including credentials, web egress, and the
Langfuse login, is in [`docs/operations.md`](docs/operations.md).

See [`cli/README.md`](cli/README.md) for the complete command reference, and the
[README](README.md#which-target-do-i-want) for a table to help you pick a target.

## Contributor path (repo checkout)

Working on AgentOS itself, rather than just building a plugin against it? Run
from a repo checkout against the dev stack instead of the prebuilt binary:

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

# 4. Build the CLI and the runner image
cd cli && cargo build --release && cd -
agentos build

# 5. Boot the API server (needs the Postgres schema applied once)
cd apps/api && uv run alembic upgrade head
uv run uvicorn agentos_api.main:app --port 8000 &
cd -

# 6. Boot the UI (fixture mode by default; ?api=1 wires it to the running API)
cd apps/ui && pnpm install && pnpm dev
# open http://localhost:5173/?state=1        (fixture demo)
# open http://localhost:5173/?api=1&state=1  (wired to apps/api on :8000)
```

`agentos local up` publishes the API on `:28000` (the compose host port); the
hand-run `uvicorn` in step 5 uses `:8000` instead — point a hand-run `local
deploy --api-url` at whichever one you brought up.

See [`AGENTS.md`](AGENTS.md) for the full per-package verify commands and dev
stack gotchas, [`docs/onboarding.md`](docs/onboarding.md) for a from-scratch
walkthrough of the `skill`/`local` tiers geared at onboarding, and
`apps/api/README.md` / `apps/ui/README.md` for the API and UI's own docs.
