# cli

The `agentos` CLI (Rust: clap + tokio + reqwest). It speaks
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
| `agentos eval` | Run `evals/cases.json` through the runner as `eval_case` events; prints a per-case result table plus a pass/fail roll-up; nonzero exit on failure. |
| `agentos steer "..."` | Inject a follow-up into the runner's live turn (POST `/v1/steer`); prints `no active turn` and exits nonzero on the runner's 409. |
| `agentos interrupt` | Hard-stop the runner's live turn (POST `/v1/interrupt`); `--reason` is recorded with it. |
| `agentos chat "..."` | Drive the whole system with the CLI acting **as** the Slack service against the local compose stack, no Slack involved (see below). |
| `agentos message "..."` | Drive the **deployed** Kubernetes release end to end with zero Slack: self-plumbs kubectl port-forwards, points the deployed worker at a local Slack stub (`helm upgrade --reuse-values`), enqueues, and prints the reply (see below). |
| `agentos message --local "..."` | Same roundtrip against the **local compose stack** (`agentos local up`) instead of a cluster: no kubectl/helm/port-forwards. Enqueues straight to the compose Valkey and lets the containerized worker answer. Channel via `--channel` or the sole deployed agent (looked up on the compose API, default `http://localhost:28000`, overridable with `--api-url`). |
| `agentos status` / `agentos stop` | Session status / tear down the container. |
| `agentos deploy` | Package the bundle as tar.gz and push it to the platform API (find-or-create agent, create version, upload bundle, create deployment). Auth via `--api-key` / `AGENTOS_API_KEY`. |

`start` records the container in the bundle's `.agentos/runner.json`
(gitignored by the scaffold); `send`/`eval`/`status`/`stop`/`steer`/`interrupt`
run from the bundle directory and resolve the runner from it, or accept `--url`.
`start --model <id>` forwards `AGENTOS_MODEL` into the container (omit for the
SDK default); setting it makes token usage attributable in Langfuse traces.

## Output

Three global flags apply to every subcommand: `--debug` shows the verbose
plumbing (helm/kubectl/compose command lines and their output, as dim lines),
`-q`/`--quiet` prints the payload only (suppressing all progress and diagnostics
on stderr), and `--color <auto|always|never>` (default `auto`) controls ANSI
color.

Stream discipline is strict: the **payload** (streamed agent reply tokens,
resolved URLs, the status table, eval results, the deploy result, `runner-status`
JSON, the worker reply) goes to **stdout**, and every **diagnostic**
(waiting/helm/kubectl/rollout/port-forward chatter, spinners, progress, notes)
goes to **stderr**. So the payload pipes and redirects cleanly while progress
still shows on the terminal:

```bash
agentos message "..." | jq        # clean JSON on stdout, progress on stderr
agentos message "..." > reply.txt  # reply captured, progress on the terminal
agentos eval > results.txt         # results captured, progress on the terminal
```

On an interactive terminal, progress renders as a spinner-to-checkmark checklist
(each step spins with a live dim elapsed counter, then freezes to a green `✓` or
red `✗` with its elapsed time), a determinate bar for real totals (eval
`N/total`), and streamed tokens that spin only until the first token then stream
raw to stdout. Every wait resolves: a blown timeout ends in `✗ ... timed out
after Ns`, never a hang. Compatibility is handled automatically:

- **Auto-disable off a TTY.** Rendering is gated on `stderr.is_terminal()` plus
  the cross-tool env standards. On a non-TTY, a pipe, `CI`, `TERM=dumb`,
  `NO_COLOR`, or `CLICOLOR=0`, output is plain discrete status lines with no ANSI
  and no `\r` redraws. `CLICOLOR_FORCE` / `--color=always` force color on;
  `--color=never` forces it off. Color is resolved per stream, so a colored
  terminal stderr never leaks ANSI into a redirected stdout.
- **Graceful degradation.** The brand palette (success green, error red, amber
  warn, dim grey plumbing, cyan URLs/ids, bold payload) is truecolor, degrading
  to the 16 named ANSI colors where truecolor is unsupported (Apple Terminal,
  tmux without passthrough).
- **Never color-only.** Every status pairs a glyph with a word (`✓ pass`,
  `✗ fail`, `⚠ warn`), and glyphs fall back to ASCII (`v`/`x`/`!`, `- \ | /`
  spinner) in non-UTF-8 locales.

## `agentos chat`: the CLI as the Slack service

`chat` exercises the real ingress-to-egress path with **no Slack at all**. It
stands up a minimal Slack Web API stub locally, `XADD`s the exact
`QueuedSlackEvent` the dispatcher would produce onto the real Valkey stream
(synthetic `EvSIM-` ids and, by default, an invented internally-consistent
channel plus thread/placeholder timestamps), then waits for the worker to
consume and finalize the turn. Completion is the worker's XACK of the stream
entry, not a timing guess: the worker acks only after the turn finalizes, so the
latest `chat.update` the stub captured is the final reply (avoiding a throttled
interim edit being mistaken for the answer). It prints the reply and exits 0, or
on timeout prints stream diagnostics (`XLEN` + `XINFO GROUPS` + `XPENDING`) and
exits nonzero.

### Targeting a deployed agent and continuing a thread

The worker binds a channel to an agent by **exact equality** on
`agents.slack_channel`, so a random synthetic channel can never reach a deployed
agent. Use `--channel <id>` to send as a specific channel: pass the same value
you gave `deploy --slack-channel` and the worker routes the turn to that agent.
Omit `--channel` to keep the old behavior (a throwaway synthetic channel).

```bash
agentos deploy --slack-channel CSIM123 ...
agentos chat --channel CSIM123 "first question"
```

Each turn mints a fresh thread ts by default, so a multi-turn conversation is
otherwise impossible. On completion `chat` prints a `continue this
conversation: ...` line with the channel and thread ts; copy-paste it (or pass
`--thread <ts>` yourself) to send the next turn into the same thread:

```bash
agentos chat --channel CSIM123 --thread 1720000000.000100 "follow-up question"
```

Contract: run the worker with `SLACK_API_BASE_URL` pointing at the `/api/` base
URL `chat` prints on startup. The worker reads that env var and points its Slack
sink's `AsyncWebClient` `base_url` at it, so `chat.update` edits land at the stub
instead of real Slack. Use `--listen-host`/`--listen-port` when the worker runs
off-box (default `localhost` on an ephemeral port). No Slack token or real Slack
HTTP is involved. The full worker round trip is validated end to end against a
live cluster; `chat` itself verifies the stub, the enqueue, and the ack-based
completion.

## `agentos message`: drive the deployed cluster with zero Slack

`message` is `chat`'s engine with Kubernetes-aware auto-plumbing on top. Where
`chat` targets a local compose stack you run yourself, `message` targets a
**deployed** Helm release and wires everything itself, so a developer building an
agent for **someone else's** Slack workspace can exercise the whole deployed
machinery (Valkey queue -> worker -> claimed sandbox -> the real skill -> the
reply) without any Slack access, tokens, or workspace.

```bash
agentos message "summarize the latest deploy"          # single deployed agent
agentos message --channel CSIM123 "another question"   # pick the agent explicitly
```

What it does, in order:

1. **Self-managed port-forwards** (children of the CLI, killed on exit): the
   in-cluster Valkey (`svc/<release>-valkey`, local `56381`) for the enqueue, and
   the API (`svc/<release>-api`, local `8123`) only when `--channel` is omitted,
   to look up the default channel.
2. **Channel default**: with no `--channel`, `GET /agents` and use the sole
   deployed agent's `slack_channel`. Zero or multiple agents is an error naming
   them and requiring `--channel` (the worker binds a channel to an agent by
   exact equality, so guessing would route nowhere).
3. **Reachable stub**: binds `0.0.0.0:<--listen-port>` (default `8155`) and
   advertises a routable host so the in-cluster worker can post back to it.
   `--listen-host` wins; otherwise the local IP the kernel would use to reach the
   cluster is auto-detected.
4. **Worker wiring** (`--wire`, the default): points the deployed worker at the
   stub via `helm upgrade --reuse-values --set worker.slackApiBaseUrl=<url>` (take
   `--chart` like the other ops verbs) and waits for the rollout. `--no-wire`
   instead refuses to run unless the worker is already wired, printing the exact
   command to apply.
5. **Safety guard**: if the release is connected to a real Slack workspace (a
   `<release>-dispatcher` deployment exists, which only renders when both Slack
   tokens are set), wiring is refused unless `--force-wire`, since pointing the
   worker at the stub would hijack that workspace's replies cluster-wide. In the
   demo flow `message` runs **before** a real Slack workspace is connected, so the
   guard never fires; and the helm upgrade that connects Slack (setting
   `worker.slackApiBaseUrl=` to empty in the same command) un-wires the stub when
   real Slack is connected.
6. **Enqueue + wait**: `XADD`s the exact `QueuedSlackEvent`, waits for the worker
   to finalize (the same ack-based completion `chat` uses), prints the reply, and
   emits a `continue this conversation: ...` line for multi-turn threads. On
   timeout it prints stream diagnostics and exits nonzero.

`--dry-run` prints the kubectl/helm command lines, the stub URL, and the enqueue
description without executing anything.

### `--local`: the same roundtrip against the compose stack

`agentos message --local` drives the local compose stack (`agentos local up`)
instead of a Kubernetes release, so the whole loop is one machine with no
cluster:

```bash
agentos local up
agentos deploy --plugin-dir <dir> --slack-channel C-DEMO --api-url http://localhost:28000
agentos message --local "what changed in the last deploy?"
```

Local mode keeps only the shared engine (stub + `QueuedSlackEvent` enqueue +
ack-based completion) and drops every cluster-specific step: no kubectl, no
`helm upgrade` wiring, no port-forwards, no dispatcher guard. It enqueues
straight to the compose Valkey (`localhost:26379`) and the containerized
`agentos-worker` service (already pointed at the stub via a fixed
`SLACK_API_BASE_URL=http://localhost:8155/api/`) answers by claiming a runner
container on the host Docker daemon. Channel comes from `--channel` or, when
omitted, the sole deployed agent looked up on the compose API (`--api-url`,
default `http://localhost:28000`; the API is reached directly, so no `/api`
suffix). `--local` composes with `--channel`/`--thread`/`--timeout-secs` and
rejects the cluster-only flags (`--namespace`, `--release`, `--force-wire`, ...)
with a clear error. The compose worker runs the fake model by default (a canned
reply, no credentials); export a credential and set `AGENTOS_FAKE_MODEL=0` in the
compose environment for a real model.

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
