# Local Dev Onboarding

Use this walkthrough to go from a fresh clone to a verified local turn. Stay on
the `skill` and `local` targets for onboarding. They need no Slack workspace and
no Kubernetes cluster.

## Prerequisites

Install the local path tools:

- `uv`, the Python workspace manager, and Python `3.13`. The workspace requires
  `>=3.13`.
- Docker with Compose v2, for the dev stack and the local runner container.
- An `agentos` CLI binary. Download a prebuilt binary from
  [GitHub Releases](https://github.com/curie-eng/agentos/releases), asset
  `agentos-x86_64-unknown-linux-gnu` or `agentos-aarch64-apple-darwin`, or build
  from source with the Rust stable toolchain, edition `2021`:

```bash
cd cli && cargo build --release
```

The source build writes the binary to `cli/target/release/agentos`. Put the
binary on `PATH`; the commands below assume `agentos` resolves.

`kubectl` and `helm` are not needed for local dev. They are only for the
`cluster` target.

If you build the CLI from source, build the runner image once from the repo
root:

```bash
docker build -f runner/Dockerfile -t agentos-runner .
```

A released binary instead pulls the pinned runner image from `GHCR` on first
run, so only source builds need this.

## The Three Tiers

| Target | What runs | Slack | Kubernetes | Reach for it to |
|---|---|---|---|---|
| `skill` | Just the runner container on the host Docker daemon. No platform, no queue, no API, no Slack. Fully offline. | none | none | Iterate a plugin/skill against a local runner, the fastest loop. |
| `local` | The full platform via docker compose (Postgres, Valkey, API, worker, and by default Langfuse plus the UI). | stub by default | none | Exercise the real queue -> worker -> sandbox -> reply product loop with zero Slack and zero Kubernetes. |
| `cluster` | The platform on Kubernetes (a Helm release). | optional | yes | Operate and drive a deployed cluster release (see `docs/operations.md`). |

During onboarding, start with `skill` for the fastest proof that a bundle can
answer. Move to `local` for ticket verification, because it puts the full
platform in front of the runner without Slack or Kubernetes.

The distinction that matters: `skill` is the runner only loop. It talks straight
to the runner container's ACI HTTP surface, with no platform in front. `local`
and `cluster` put the full platform (queue, worker, sandbox) in front of the
identical runner, so a `message` walks the same path a real Slack mention would.
Every environment command takes a target noun in the middle:
`agentos <skill|local|cluster> <verb>`. `agentos init` is the exception: it
scaffolds a bundle and targets no environment.

## Step 1: Fastest First Reply With `skill`

No credential, no Slack, no cluster, no compose stack:

```bash
agentos init my-agent && cd my-agent
agentos skill up --fake-model
agentos skill message "hello"
agentos skill down
```

`agentos init` scaffolds a runnable weather example plugin bundle: a Claude Code
plugin with `.claude-plugin/plugin.json`, `skills/<name>/SKILL.md`, `.mcp.json`,
and an `evals/cases.json` seed. `--fake-model` gives scripted replies with no
Anthropic key, so this proves the loop offline. Drop `--fake-model` and export a
credential, `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, or
`AGENTOS_CREDENTIALS`, for a real model.

The reply streams to `stdout` as `NDJSON`. Abort a live turn with `Ctrl-C`.
`agentos skill eval` runs the bundle's `evals/cases.json` the same way.

## Step 2: The Real Product Loop With `local`

Use this loop to verify tickets. It drives a message through the real queue,
worker, sandboxed runner, and reply path with no Slack and no Kubernetes.

```bash
# From the bundle directory (my-agent). Bring up the full compose stack:
agentos local up

# Deploy the bundle to the compose platform API (published on host port 28000):
agentos local deploy --plugin-dir . --slack-channel C-DEMO --api-url http://localhost:28000

# Drive a turn through the real path and read the reply on stdout:
agentos local message "what changed in the last deploy?"
```

- `agentos local up` brings up the `full` compose profile by default: the backing
  stores (`Postgres`, `Valkey`, `MinIO`), the `API`, the `worker`, plus
  `Langfuse` and the console UI. The console UI is served at
  `http://localhost:28080/?api=1`. The `API` is published on host port `28000`,
  so point
  `local deploy --api-url` there.
- The compose `worker` runs the fake model by default, a canned reply with no
  credentials, so this whole loop needs zero secrets. For a real model, export a
  credential, `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`, and set
  `AGENTOS_FAKE_MODEL=0` in the compose environment.
- `--slack-channel C-DEMO` binds the agent to a channel id. Pass the same value
  to `local message --channel C-DEMO` to target it. When a single agent is
  deployed, `local message` looks up the channel automatically, so `--channel`
  is optional then.
- For multi turn work, `local message` prints a `continue this conversation:`
  line. Use `agentos local message --continue "follow up"` to reuse the last
  turn's channel and thread from `.agentos/last-turn.json`. Send only the new
  text. `--continue` reads the API key again from `$AGENTOS_API_KEY` and does not
  replay transport flags like `--listen-port`, so pass those again if you used
  custom values.

## Step 3: Low RAM Machines

```bash
agentos local up --minimal
```

`--minimal` brings up the smaller `core` compose profile: `Postgres`, `Valkey`,
`MinIO`, the `API`, and the `worker` only. It drops `Langfuse`, `ClickHouse`,
the `OTel Collector`, and the console UI, which are present only in the heavier
`full` profile. Use it for a low RAM laptop. The `deploy` and `message` steps
above are identical against `--minimal`; you just lose the UI and `Langfuse`
traces.

Only one local compose stack runs at a time.

## Step 4: Verify a Ticket End to End

Use this as the verification loop for a code change:

1. Rebuild what changed. Rebuild the runner image with
   `docker build -f runner/Dockerfile -t agentos-runner .` if you touched runner
   code. Run
   `agentos local deploy --plugin-dir . --slack-channel C-DEMO --api-url http://localhost:28000`
   again to push a changed bundle.
2. Drive a real turn with `agentos local message "..."`. It walks the full
   queue, worker, sandboxed runner, and reply path, the same path a Slack mention
   takes.
3. Observe the result three ways:
   - The reply prints to **`stdout`**. Redirect it with
     `agentos local message "..." > reply.txt`; progress goes to `stderr`, so it
     pipes cleanly.
   - The `worker` and `runner` log to `stdout`, so
     `docker compose -f compose.dev.yaml logs -f agentos-worker`, or the runner
     container, makes the turn observable.
   - On the `full` profile, the turn is traced in `Langfuse`. This is skipped on
     `--minimal`.
4. `agentos local status` confirms the compose services are healthy, with
   `docker compose ps` under the hood. `agentos local down` stops the stack and
   keeps volumes.

This is a complete end to end verification with no Slack and no cluster, which
is the point of the `local` target.

## Where to Go Next

- [`QUICKSTART.md`](../QUICKSTART.md): first reply in about a minute.
- [`cli/README.md`](../cli/README.md): full command and flag reference for all
  three targets.
- [`docs/operations.md`](operations.md): the `cluster` target runbook and the
  full tier table.
- [`AGENTS.md`](../AGENTS.md): repo rules, verify commands, and dev stack
  gotchas.
- [`docs/vision.md`](vision.md): what AgentOS is and why.
