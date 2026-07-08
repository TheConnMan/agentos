# cli

The `agentos` CLI (Rust: clap + tokio + reqwest). It speaks
only the frozen contracts (the generated `agentos-aci-protocol` crate over
HTTP/NDJSON, and the platform API's committed openapi.json) and orchestrates a
local runner container via Docker, so a plugin runs on a dev laptop with zero
Slack involved.

## Which target do I want?

Every environment command takes a **target noun** in the middle: `skill`,
`local`, or `cluster`. Pick the lightest one that answers your question.
`agentos init` is the exception, a top-level verb that scaffolds a bundle on
disk and targets no environment.

| Target | What runs | Slack | Kubernetes | Verbs | Reach for it to |
|---|---|---|---|---|---|
| `skill` | Just the runner container on the host Docker daemon. No platform, no queue, no API, no Slack. Fully offline. | none | none | `up` `down` `status` `message` `eval` | Iterate a plugin/skill against a local runner, the fastest loop. |
| `local` | The full platform via docker compose (Postgres + Valkey + Langfuse + API + worker). | stub by default, optional real Slack with `--slack` | none | `up` `down` `status` `message` `deploy` | Exercise the real queue -> worker -> sandbox -> reply product loop with zero Slack and zero Kubernetes. Its API is published on host port `28000`. |
| `cluster` | The platform on Kubernetes (a Helm release). | optional | yes | `up` `down` `status` `message` `deploy` | Operate and drive a deployed cluster release. |

The universal quartet `up`/`down`/`status`/`message` is on all three targets;
`skill` adds `eval`, and `local`/`cluster` add `deploy`. The distinction that
matters: `skill` is the **runner-only** loop, talking straight to a runner
container's ACI HTTP surface with no platform in front; `local` and `cluster`
put the **full platform** (queue, worker, sandbox) in front of the identical
runner and ACI, so a `message` walks the same path a real Slack mention would.

## `init` (top-level)

| Command | What it does |
|---|---|
| `agentos init <name>` | Scaffold a plugin bundle (Claude Code plugin shape: `.claude-plugin/plugin.json`, `skills/<name>/SKILL.md`, `.mcp.json`) plus an `evals/cases.json` seed. |

## `skill` target: runner-only, fully offline

Boots just the runner container on the host Docker daemon and speaks its ACI
HTTP surface directly. No platform, no queue, no API, no Slack, no cluster.

| Command | What it does |
|---|---|
| `agentos skill up` | Boot the local runner image in Docker with the ACI boot env (runner/README.md recipe), wait for health, print the boxed env summary. `--fake-model` runs offline; `--network` and `--otel-endpoint` join the compose stack for traces; `--model <id>` forwards `AGENTOS_MODEL` (omit for the SDK default). |
| `agentos skill message "..."` | Send a synthetic Slack event: POST an ACI `event` frame to the local runner and stream the NDJSON reply (text deltas, tool notes, side effect flags, final). Abort a live turn with Ctrl-C. |
| `agentos skill eval` | Run `evals/cases.json` through the runner as `eval_case` events; prints a per case result table plus a pass or fail rollup; nonzero exit on failure. |
| `agentos skill status` | Show the local runner's session status. |
| `agentos skill down` | Stop and remove the local runner container. |

`skill up` records the container in the bundle's `.agentos/runner.json`
(gitignored by the scaffold); `skill message` / `skill eval` / `skill status` /
`skill down` run from the bundle directory and resolve the runner from it, or
accept `--url`. Setting `skill up --model <id>` makes token usage attributable
in Langfuse traces.

## `local` target: full platform via compose, no Slack

Wraps the `compose.dev.yaml` stack so a `message` walks the real
queue -> worker -> sandboxed runner -> reply path on one machine, no Slack and
no Kubernetes. `agentos local up` uses the `full` compose profile by default.
`agentos local up --minimal` uses the smaller `core` profile. The compose API is
published on host port `28000`. Add `agentos local up --slack` to also start
the optional Slack dispatcher.

| Command | What it does |
|---|---|
| `agentos local up` | Bring the compose stack up (`docker compose --profile full up -d --wait` by default, `docker compose --profile core up -d --wait` with `--minimal`) and print URLs. Add `--slack` to append `--profile slack`. |
| `agentos local down` | Stop the compose stack (`docker compose down`), keeping volumes. |
| `agentos local status` | Show the compose stack's service status (`docker compose ps`). |
| `agentos local message "..."` | Drive the local compose stack end to end with zero Slack. Enqueues straight to the compose Valkey and lets the containerized worker answer. |
| `agentos local deploy` | Package the bundle as tar.gz and push it to the compose platform API (`--api-url`, default `http://localhost:28000`). Auth via `--api-key` or `AGENTOS_API_KEY`. |

## `cluster` target: deployed Helm release

Wraps the umbrella Helm chart and the deployed release, the way `linkerd` or
`cilium` wrap theirs. Every operator verb takes `--dry-run`. Full runbook in
[`docs/operations.md`](../docs/operations.md).

| Command | What it does |
|---|---|
| `agentos cluster up` | Install or upgrade the release (`helm upgrade --install`). Exposes the UI and Langfuse on node ports; `--no-expose` keeps them ClusterIP-only. Set `AGENTOS_MODEL_CREDENTIALS` for a real model, or install sealed with canned replies. |
| `agentos cluster down` | Uninstall the release and sweep its runtime namespaces (`helm uninstall` + `kubectl delete namespace`); prompts unless `--yes`. |
| `agentos cluster status` | Report release health, pod readiness, and access URLs (read-only). |
| `agentos cluster message "..."` | Drive the deployed release end to end with zero Slack: self plumbs kubectl port forwards, points the deployed worker at a local Slack stub (`helm upgrade --reuse-values`), enqueues, and prints the reply. |
| `agentos cluster deploy` | Package the bundle as tar.gz and push it to the platform API (`--api-url`, default `http://localhost:8000`). Auth via `--api-key` or `AGENTOS_API_KEY`. |

### Artifact resolution

Release builds resolve default artifacts from the binary version: `agentos local
up` fetches the self contained `compose.release.yaml` release asset, so it
works with no checkout, `agentos cluster up` uses the pinned chart release
asset, and runner sessions (`agentos skill up`) use the pinned GHCR runner
image. Fetched artifacts cache under
`${XDG_CACHE_HOME:-$HOME/.cache}/agentos/<version>/`, so repeated
`agentos cluster up` and `agentos local up` reuse the cache.

Dev builds use the local `compose.dev.yaml`, `charts/agentos`, and
`agentos-runner` when present. A dev binary run with no local artifact errors,
telling you to pass `-f <compose>`, `--chart <path>`, or `--image <ref>` (or use
a released binary); those same flags override the defaults. `--dry-run` prints
the resolved argv without fetching.

`agentos cluster message` is not yet wired through this resolver: it still defaults
`--chart` to the repo-relative `charts/agentos`, so a no-checkout binary must
pass `--chart <path-or-tgz>` explicitly for now.

## Output

Three global flags apply to every subcommand: `--debug` shows the verbose
plumbing (helm/kubectl/compose command lines and their output, as dim lines),
`-q`/`--quiet` prints the payload only (suppressing all progress and diagnostics
on stderr), and `--color <auto|always|never>` (default `auto`) controls ANSI
color.

Stream discipline is strict: the **payload** (streamed agent reply tokens,
resolved URLs, the status table, eval results, the deploy result, `skill status`
JSON, the worker reply) goes to **stdout**, and every **diagnostic**
(waiting/helm/kubectl/rollout/port-forward chatter, spinners, progress, notes)
goes to **stderr**. So the payload pipes and redirects cleanly while progress
still shows on the terminal:

```bash
agentos cluster message "..." | jq         # clean JSON on stdout, progress on stderr
agentos local message "..." > reply.txt    # reply captured, progress on the terminal
agentos skill eval > results.txt           # results captured, progress on the terminal
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

## `agentos cluster message`: drive the deployed cluster with zero Slack

`cluster message` targets a **deployed** Helm release and wires everything
itself, so a developer building an agent for someone else's Slack workspace can
exercise the whole deployed machinery (Valkey queue -> worker -> claimed
sandbox -> the real skill -> the reply) without any Slack access, tokens, or
workspace.

```bash
agentos cluster message "summarize the latest deploy"
agentos cluster message --channel CSIM123 "another question"
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
   to finalize, prints the reply, and emits a `continue this conversation: ...`
   line for multi turn threads. On timeout it prints stream diagnostics and
   exits nonzero.

`--dry-run` prints the kubectl/helm command lines, the stub URL, and the enqueue
description without executing anything.

Use `--continue` to reuse the last successful `cluster message` context from
`.agentos/last-turn.json` in the current working directory, so only the new text
is required. Explicit flags override the saved channel, thread, and transport
settings, the verb must match, and the API key is re-read from
`$AGENTOS_API_KEY` because the value is never stored. Note that `--continue`
does not replay `--stream`, `--listen-port`, `--valkey-local-port`,
`--api-local-port`, or `--user`, so pass any of those again explicitly if the
original turn used a non-default value.

### Targeting a deployed agent and continuing a thread

The worker binds a channel to an agent by exact equality on
`agents.slack_channel`, so a random synthetic channel can never reach a
deployed agent. Use `--channel <id>` to send as a specific channel: pass the
same value you gave `cluster deploy --slack-channel` and the worker routes the
turn to that agent.

```bash
agentos cluster deploy --slack-channel CSIM123 ...
agentos cluster message --channel CSIM123 "first question"
```

Each turn mints a fresh thread ts by default. On completion `cluster message`
prints a `continue this conversation: ...` line with the channel and thread ts;
copy paste it, or pass `--thread <ts>` yourself, to send the next turn into the
same thread:

```bash
agentos cluster message --channel CSIM123 --thread 1720000000.000100 "follow up question"
```

## `agentos local message`: the same roundtrip against the compose stack

`local message` drives the local compose stack (`agentos local up`) instead of a
Kubernetes release, so the whole loop is one machine with no cluster:

```bash
agentos local up
agentos local deploy --plugin-dir <dir> --slack-channel C-DEMO --api-url http://localhost:28000
agentos local message "what changed in the last deploy?"
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
suffix). `local message` composes with `--channel`, `--thread`, and
`--timeout-secs` and rejects the cluster only flags (`--namespace`,
`--release`, `--force-wire`, ...)
with a clear error. The compose worker runs the fake model by default (a canned
reply, no credentials); export a credential and set `AGENTOS_FAKE_MODEL=0` in the
compose environment for a real model.

Use `--continue` to reuse the last successful `local message` context from
`.agentos/last-turn.json` in the current working directory, so only the new text
is required. Explicit flags override the saved channel, thread, and transport
settings, the verb must match, and the API key is re-read from
`$AGENTOS_API_KEY` because the value is never stored. Note that `--continue`
does not replay `--stream`, `--listen-port`, `--valkey-local-port`,
`--api-local-port`, or `--user`, so pass any of those again explicitly if the
original turn used a non-default value.

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
