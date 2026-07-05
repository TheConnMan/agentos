# cli

Owning task: **I1**. The `agentos` CLI (Rust: clap + tokio + reqwest). It speaks
only the frozen contracts (the generated `agentos-aci-protocol` crate over
HTTP/NDJSON, and the platform API's committed openapi.json) and orchestrates a
local runner container via Docker, so a plugin runs on a dev laptop with zero
Slack involved.

## Commands

| Command | What it does |
|---|---|
| `agentos init <name>` | Scaffold a plugin bundle (Claude Code plugin shape: `.claude-plugin/plugin.json`, `skills/<name>/SKILL.md`, `.mcp.json`) plus an `evals/cases.json` seed. |
| `agentos start` | Boot the runner image in Docker with the ACI boot env (runner/README.md recipe), wait for health, print the boxed env summary. `--fake-model` runs offline; `--network`/`--otel-endpoint` join the compose stack for traces. |
| `agentos send "..."` | Emulate a Slack message: POST an ACI `event` frame to the local runner and stream the NDJSON reply (text deltas, tool notes, side-effect flags, final). |
| `agentos eval` | Run `evals/cases.json` through the runner as `eval_case` events; per-case pass/fail lines and a summary; nonzero exit on failure. |
| `agentos steer "..."` | Inject a follow-up into the runner's live turn (POST `/v1/steer`); prints `no active turn` and exits nonzero on the runner's 409. |
| `agentos interrupt` | Hard-stop the runner's live turn (POST `/v1/interrupt`); `--reason` is recorded with it. |
| `agentos chat "..."` | Drive the whole system with the CLI acting **as** the Slack service, no Slack involved (see below). |
| `agentos slack-sim "..."` | The real-Slack egress rung: post a synthetic thread as the bot in a real channel (`--channel`, `SLACK_BOT_TOKEN`), enqueue, and poll `conversations.replies` until the worker edits the placeholder. Use when validating real Slack egress without Socket Mode. |
| `agentos status` / `agentos stop` | Session status / tear down the container. |
| `agentos deploy` | Package the bundle as tar.gz and push it to the platform API (find-or-create agent, create version, upload bundle, create deployment). Auth via `--api-key` / `AGENTOS_API_KEY`. |

`start` records the container in the bundle's `.agentos/runner.json`
(gitignored by the scaffold); `send`/`eval`/`status`/`stop`/`steer`/`interrupt`
run from the bundle directory and resolve the runner from it, or accept `--url`.
`start --model <id>` forwards `AGENTOS_MODEL` into the container (omit for the
SDK default); setting it makes token usage attributable in Langfuse traces.

## `agentos chat`: the CLI as the Slack service

`chat` exercises the real ingress-to-egress path with **no Slack at all**. It
stands up a minimal Slack Web API stub locally, `XADD`s the exact
`QueuedSlackEvent` the dispatcher would produce onto the real Valkey stream
(synthetic `EvSIM-` ids and invented, internally-consistent channel/thread/
placeholder timestamps), then waits for the worker to consume and finalize the
turn. Completion is the worker's XACK of the stream entry, not a timing guess:
the worker acks only after the turn finalizes, so the latest `chat.update` the
stub captured is the final reply (avoiding a throttled interim edit being
mistaken for the answer). It prints the reply and exits 0, or on timeout prints
stream diagnostics (`XLEN` + `XINFO GROUPS` + `XPENDING`) and exits nonzero.

Contract: run the worker with `SLACK_API_BASE_URL` pointing at the `/api/` base
URL `chat` prints on startup (that env var is added to the worker by another
lane). Use `--listen-host`/`--listen-port` when the worker runs off-box (default
`localhost` on an ephemeral port). No Slack token, channel, or real Slack HTTP is
involved. The full worker round trip is validated at the walking-skeleton gate;
`chat` itself verifies the stub and the enqueue.

## Verify

```bash
cd cli && cargo fmt --check && cargo clippy -- -D warnings && cargo test
```

The scripted E2E (real runner container, fake model, offline) plus an optional
deploy leg against a locally-run apps/api:

```bash
bash cli/scripts/e2e.sh
# with the compose stack + a local API:
AGENTOS_E2E_NETWORK=agentos_default \
AGENTOS_E2E_OTEL=http://otel-collector:4318 \
AGENTOS_E2E_API_URL=http://localhost:8000 bash cli/scripts/e2e.sh
```

Requires an `agentos-runner` image (`docker build -f runner/Dockerfile -t
agentos-runner .` from the repo root).
