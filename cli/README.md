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
| `skill` | Just the runner container on the host Docker daemon. No platform, no queue, no API, no Slack. Fully offline. | none | none | `up` `check` `down` `status` `message` `eval` | Iterate a plugin/skill against a local runner, the fastest loop. |
| `local` | The full platform via docker compose (Postgres + Valkey + Langfuse + API + worker). | stub by default, optional real Slack with `--slack` | none | `up` `down` `status` `comms` `message` `eval` `deploy` | Exercise the real queue -> worker -> sandbox -> reply product loop with zero Slack and zero Kubernetes. Its API is published on host port `28000`. |
| `cluster` | The platform on Kubernetes (a Helm release). | optional | yes | `up` `down` `status` `comms` `message` `eval` `deploy` `kill` `resume` `budget` `delete` | Operate and drive a deployed cluster release, and control its agents' lifecycle. |

The universal quartet `up`/`down`/`status`/`message` is on all three targets;
`skill` adds `eval`, while `local` and `cluster` add `comms`, `eval`, plus `deploy`; `cluster`
further adds the agent-lifecycle verbs `kill`/`resume`/`budget`/`delete`. `eval` is on
all three: it runs the SAME `evals/cases.json` with the SAME grader at each tier (the
per-tier parity gate), so a suite that passes at `skill` can be re-asserted verbatim at
`local` and `cluster`. The distinction
that matters: `skill` is the **runner-only** loop, talking straight to a runner
container's ACI HTTP surface with no platform in front; `local` and `cluster`
put the **full platform** (queue, worker, sandbox) in front of the identical
runner and ACI, so a `message` walks the same path a real Slack mention would.

## `init` (top-level)

| Command | What it does |
|---|---|
| `agentos init <name>` | Scaffold a plugin bundle (Claude Code plugin shape: `.claude-plugin/plugin.json`, `skills/<name>/SKILL.md`, `.mcp.json`) plus an `evals/cases.json` seed, a root `AGENTS.md`, and an installable `.claude/skills/using-agentos/SKILL.md` harness primer. |
| `agentos init --from-spec <path>` | Scaffold **non-interactively** from an agent-authored spec file (JSON). The bundle name comes from the spec, not a positional argument. A coding agent interviews the human, writes the spec, then this command lays down the same plugin-format shape deterministically -- zero prompts. See the spec shape below. |
| `agentos` | Open the keyboard-driven terminal interface. Explicit forms: `agentos interactive`, `agentos ui`, `agentos tui`. |
| `agentos secrets set <NAME>` | Save a local secret in AgentOS's mode-0600 credential file with hidden input. `--from-env <VAR>` reads from an existing environment variable for non-interactive use without putting the value in argv. |
| `agentos secrets list` | List saved AgentOS secret names. Values are never printed. |
| `agentos secrets unset <NAME>` | Remove a saved local secret. |
| `agentos guide` | Print a self-contained primer (ADR-0021) for a coding agent driving the harness: the parity ladder, when/which decision logic, the landmines, and verify-first, to stdout. `--json` emits the same content as a structured variant (data on stdout). |
| `agentos build` | Build the runner image locally: `docker build -f runner/Dockerfile -t agentos-runner .` from the repo root (found by walking up to `runner/Dockerfile`). `--tag` overrides the tag. Prints a clear error if Docker is not installed or if run outside a source checkout -- a release binary pulls the pinned runner image from GHCR automatically and never needs to build. |

### `init --from-spec` spec shape

The spec is a JSON object an agent writes after interviewing the human. `name`
is the kebab-case bundle name; every `skills[].name` is kebab-case and unique;
`connectors` (optional) is the raw `.mcp.json` `mcpServers` map (each server must
define `command` or `url` as a string); `secrets` (optional) is a list of
connector-secret NAMES (env-var-shaped, no values, per ADR-0009) written to the
manifest's `secrets`; `approvalPolicy` (optional) declares approval `gates`
(`{gate, route}`) where an `mcp__` gate must be a fully-namespaced live tool name
`mcp__plugin_<bundle>_<server>__<tool>` for a declared connector (a built-in like
`Bash` needs no prefix) — so a spec can express a gated, authed agent without
hand-editing `plugin.json`; `evals` reuses the frozen eval-case shape
so the scaffolded `evals/cases.json` loads unchanged through `agentos skill eval`.
An unknown TOP-LEVEL field is a hard error, so an authoring typo fails loud, but
unknown keys INSIDE an eval case are ignored exactly as the platform's worker
`EvalSuite` ignores them (pydantic `extra="ignore"`), which is intentional parity
with the platform grader, not an oversight.

```json
{
  "name": "deal-desk",
  "description": "Prices and reviews deal desk requests.",
  "skills": [
    {
      "name": "deal-desk",
      "description": "Invoke when a rep submits a pricing exception request.",
      "allowed_tools": ["WebSearch", "WebFetch"],
      "instructions": "Price the exception against the guardrails, then summarize the decision.\n"
    }
  ],
  "connectors": {
    "crm": { "command": "crm-mcp", "args": ["--stdio"] }
  },
  "secrets": ["CRM_API_TOKEN"],
  "approvalPolicy": {
    "gates": [
      { "gate": "mcp__plugin_deal-desk_crm__create_deal", "route": "default" }
    ]
  },
  "evals": [
    {
      "id": "prices-a-deal",
      "input": "Quote 20% off for Acme",
      "grader": { "kind": "contains", "expected": "approved", "case_sensitive": false }
    }
  ]
}
```

```bash
agentos init --from-spec agent-spec.json   # bundle name (deal-desk) comes from the spec
```

## `agentos` / `agentos interactive`

The interactive terminal interface is a human-friendly command surface over the
same `agentos ...` subcommands documented here. It opens a full-screen TUI with
target navigation, action selection, command previews, and guarded execution:
when an action needs values (for example message text or a channel id), the TUI
temporarily leaves the alternate screen, prompts for the values, runs the exact
previewed command, then returns to the interface. Some actions also require a
tier (local or cluster); the TUI asks which tier before prompting for the
other values, and the command preview shows `<local|cluster>` in the tier's
position until that question is answered.

```bash
agentos
agentos interactive
agentos ui
agentos tui
```

Keyboard:

| Key | Action |
|---|---|
| `Up`/`Down` or `k`/`j` | Move through actions |
| `Tab` / `Left` / `Right` | Switch target filters |
| `Enter` or `r` | Prompt for fields and run the selected command |
| `q` or `Esc` | Exit |

The first surface focuses on the common inner-loop and operations paths:
`skill up/message/eval`, an **Explore examples** picker with live agent chat,
`secrets set/list/unset`, `local up/message/status`, `cluster status/message`,
`install`, and `dev contracts`.

**Explore examples** opens a dialog for GitHub issues, Text stats engine, or
Weather. After selection, AgentOS checks that example's credentials, starts its
bundle once, and opens a persistent conversation. Type a message, read the
reply, and continue for as many turns as needed. Leaving chat stops the runner
and returns to AgentOS.

## `agentos secrets`

Local secrets are stored in `~/.config/agentos/credentials.json` with mode 0600,
not in the repo, shell history, command argv, `.env`, or AgentOS state files.
This follows the prompt-free private-config pattern used by developer CLIs.
AgentOS keeps a separate non-secret index so secret names can be listed without
opening values. Existing Keychain credentials are copied into the private file
on first use; AgentOS never writes to or deletes from Keychain during migration.

```bash
agentos secrets set GITHUB_PERSONAL_ACCESS_TOKEN
agentos secrets set ANTHROPIC_API_KEY
agentos secrets list
agentos secrets unset GITHUB_PERSONAL_ACCESS_TOKEN
```

For CI or other non-interactive setup, read from an existing environment
variable instead of prompting:

```bash
agentos secrets set GITHUB_PERSONAL_ACCESS_TOKEN --from-env GITHUB_PAT
```

`agentos skill up --secret <NAME>` first uses a real environment variable when
one is already set. If it is missing, the CLI tries the AgentOS secret store and
hydrates the process environment just long enough for Docker to forward `-e
<NAME>` into the runner. The same lookup applies to saved model credentials
(`AGENTOS_CREDENTIALS`, `ANTHROPIC_API_KEY`, or `CLAUDE_CODE_OAUTH_TOKEN`) for
live `skill up` runs.

## `agentos install`

Contributor bootstrap/update for a source checkout: install dependencies and
build, but **start nothing**. Run it after cloning; rerun `./install.sh` later to
refresh an existing checkout without reinstalling already-present artifacts.
Then `agentos local up` brings the stack up. From the repo root (found by
walking up to `runner/Dockerfile`) it runs, in order and each idempotent,
streaming output:

1. Copy `.env.example` to `.env` if `.env` is missing (otherwise left untouched).
2. `uv sync` at the repo root (needs `uv`).
3. `pnpm install` in `apps/ui` (needs `pnpm`).
4. `cargo build` in `cli` (needs `cargo`).
5. Build the runner image via `agentos build` (needs `docker`).

`agentos install --update` is the rerun path used by `./install.sh` when an
installed CLI already exists. It still refreshes dependencies and local builds,
but skips rebuilding the `agentos-runner` image if that image is already present.
`./install.sh` also skips the initial `cargo install --path cli --force` when the
installed `agentos` binary is newer than the CLI sources.

Each required tool is checked first; a missing one prints a pointer (e.g. `uv is
not installed - https://docs.astral.sh/uv/`) and stops. Run outside a source
checkout it errors clearly -- a release binary has nothing to install.

## `agentos dev`

Thin wrappers over the repo's dev scripts, so contributors get one unified
`agentos <command>` surface while the scripts stay the implementation. Each finds
the repo root, confirms the script exists, shells `bash <script>` from the root,
streams its output, and propagates its exit code. Run outside a source checkout
they error clearly -- a release binary has no dev scripts.

| Command | What it does |
|---|---|
| `agentos dev contracts` | `bash scripts/check-contracts.sh` -- check the frozen contracts. |
| `agentos dev chart-check` | `bash charts/agentos/ci/render-assertions.sh` -- render-assert the Helm chart. |
| `agentos dev e2e` | `bash cli/scripts/e2e.sh` -- the scripted CLI end-to-end test. |

## `skill` target: runner-only, fully offline

Boots just the runner container on the host Docker daemon and speaks its ACI
HTTP surface directly. No platform, no queue, no API, no Slack, no cluster.

| Command | What it does |
|---|---|
| `agentos skill up` | Boot the local runner image in Docker with the ACI boot env (runner/README.md recipe), wait for health, print the boxed env summary. `--fake-model` runs offline; `--network` and `--otel-endpoint` join the compose stack for traces; `--model <id>` forwards `AGENTOS_MODEL` (omit for the SDK default). `--secret <NAME>` forwards bundle MCP secrets by name, using AgentOS private storage when the env var is not exported. |
| `agentos skill check` | Run an offline, credential free MCP load check and report declared servers, matches, and verdict. |
| `agentos skill approvals` | View the bundle's declared `approvalPolicy` gates, read straight from `.claude-plugin/plugin.json` (or `plugin.json`); no docker, no network. `--gate <TOOL>` (repeatable) or `--clear` mutate nothing -- they print the `AGENTOS_APPROVAL_REQUIRED_TOOLS=...` assignment to export, then re-run your original `skill up` invocation with `--secret AGENTOS_APPROVAL_REQUIRED_TOOLS` added, since the runner only resolves that env once at container boot. |
| `agentos skill versions` | Not available at this tier (exit 4): `skill up` runs the bundle bytes on disk, so no deployed version is assigned. Use `agentos local versions <agent>` or `agentos cluster versions <agent>`. |
| `agentos skill memory` | Not available at this tier (exit 4): this tier configures no memory namespace. Use `agentos local memory <agent>` or `agentos cluster memory <agent>`. |
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
| `agentos local observability` | Print the local platform's observability surfaces: AgentOS Console, Langfuse UI (traces / cost / evals), and the AgentOS API base. URLs are printed only; pass `--open` to also open the browsable ones (Console, Langfuse) in a browser. `--json` never opens a browser. |
| `agentos local comms --slack` | Connect or disconnect a real Slack workspace for the compose stack. Reads `SLACK_APP_TOKEN` and `SLACK_BOT_TOKEN`, masks them in dry run output, starts or stops the dispatcher, and switches the worker between real Slack and the local stub. |
| `agentos local message "..."` | Drive the local compose stack end to end with zero Slack. Enqueues straight to the compose Valkey and lets the containerized worker answer. |
| `agentos local eval` | Run the bundle's `evals/cases.json` through the compose stack's enqueue -> worker -> sandbox -> reply path (one synthetic turn per case) and grade each captured reply with the SAME grader `skill eval` uses. Prints the identical per-case table + rollup; nonzero exit on failure. `--cases` overrides the file; `--dry-run` prints the plan. |
| `agentos local deploy` | Package the bundle as tar.gz and push it to the compose platform API (`--api-url`, default `http://localhost:28000`). Auth via `--api-key` or `AGENTOS_API_KEY`. |

## `cluster` target: deployed Helm release

Wraps the umbrella Helm chart and the deployed release, the way `linkerd` or
`cilium` wrap theirs. Every operator verb takes `--dry-run`. Full runbook in
[`docs/operations.md`](../docs/operations.md).

| Command | What it does |
|---|---|
| `agentos cluster up` | Install or upgrade the release (`helm upgrade --install`). Exposes the UI and Langfuse on node ports; `--no-expose` keeps them ClusterIP-only. Set `AGENTOS_CREDENTIALS` (deprecated alias `AGENTOS_MODEL_CREDENTIALS`) for a real model, or install sealed with canned replies. A shell `AGENTOS_MODEL` now defaults the sandbox runner model (`agentSandbox.runner.model`) for cross-tier parity with `local up`, unless an explicit `--set agentSandbox.runner.model=` is passed; a shell `AGENTOS_MODEL` that disagrees with such an explicit `--set` fails loud. |
| `agentos cluster down` | Uninstall the release and sweep its runtime namespaces (`helm uninstall` + `kubectl delete namespace`); prompts unless `--yes`. |
| `agentos cluster status` | Report release health, pod readiness, and access URLs (read-only). |
| `agentos cluster observability` | Report the release's observability surfaces (AgentOS Console, Langfuse UI, AgentOS API base), using the same NodePort discovery as `cluster status`. Degrades a missing, ClusterIP, or unresolvable surface to a note instead of failing. URLs are printed only; pass `--open` to also open the browsable ones (Console, Langfuse) in a browser. `--json` never opens a browser. `--dry-run` prints the read-only discovery commands. |
| `agentos cluster comms --slack` | Connect or disconnect a real Slack workspace with a thin `helm upgrade --reuse-values`; env-backed tokens are masked in dry-run output. |
| `agentos cluster message "..."` | Drive the deployed release end to end with zero Slack: self plumbs kubectl port forwards, points the deployed worker at a local Slack stub (`helm upgrade --reuse-values`), enqueues, and prints the reply. |
| `agentos cluster eval` | Run the bundle's `evals/cases.json` through the deployed release (self-plumbed port-forwards + per-turn reply stub, one synthetic turn per case) and grade each captured reply with the SAME grader `skill eval` uses. Prints the identical per-case table + rollup; nonzero exit on failure. `--cases` overrides the file; `--dry-run` prints the plan. |
| `agentos cluster deploy` | Package the bundle as tar.gz and push it to the platform API. Reaches the API through the deployed release's UI `/api` NodePort proxy when `--api-url` is omitted (no port-forward); pass `--api-url` / `AGENTOS_API_URL` to target it directly. Auth via `--api-key` or `AGENTOS_API_KEY`. |
| `agentos cluster kill <agent> --yes` | Kill an agent (stop its runs) via the platform API (`POST /agents/{id}/kill`). Destructive: refuses without `--yes`. |
| `agentos cluster resume <agent>` | Resume a killed agent via the platform API (`POST /agents/{id}/resume`). |
| `agentos cluster budget <agent> --limit <n>` | Set the agent's daily spend cap in USD via the platform API (`PUT /agents/{id}/budget`, `BudgetConfig.max_usd_per_day`); the per-run token cap is left at the platform default. |
| `agentos cluster delete <agent> --yes` | Delete an agent via the platform API (`DELETE /agents/{id}`). Destructive and irreversible: refuses without `--yes`. |

The four lifecycle verbs (`kill`, `resume`, `budget`, `delete`) act on a
deployed release's agents through the same platform API, defaulting `--api-url`
to `http://localhost:8000` (auth via `--api-key` or `AGENTOS_API_KEY`). Unlike
`cluster deploy`, they do not auto-discover the UI proxy -- pass `--api-url` or
port-forward the API yourself. They resolve `<agent>` (a name or id) to its API id with the
same lookup `deploy` uses. Each takes `--dry-run` (prints the plan, makes no
request); the destructive `kill`/`delete` also require `--yes`.

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

Before connecting a real workspace, `cluster message` is the zero-Slack path.
When you are ready to wire Slack onto a deployed release, use:

```bash
SLACK_APP_TOKEN=xapp-... \
SLACK_BOT_TOKEN=xoxb-... \
agentos cluster comms --slack

agentos cluster comms --slack --disconnect

SLACK_APP_TOKEN=xapp-... \
SLACK_BOT_TOKEN=xoxb-... \
agentos cluster comms --slack --dry-run
```

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
reply, no credentials); export a credential in your shell and `local up` or
`local comms` goes live automatically for a real model. Set
`AGENTOS_FAKE_MODEL=1` to force the fake model regardless of a credential
being present.

Use `agentos local comms --slack` when you want the same compose stack to talk
to a real Slack workspace. Connect reads `SLACK_APP_TOKEN` and
`SLACK_BOT_TOKEN`, masks them in printed commands, starts the dispatcher, and
points the worker at real Slack, resolving the model the same way as `local up`
(live when a credential is present, fake otherwise). `--disconnect` stops the
dispatcher and restores the local stub. `--dry-run` prints the compose command
only.

Use `--continue` to reuse the last successful `local message` context from
`.agentos/last-turn.json` in the current working directory, so only the new text
is required. Explicit flags override the saved channel, thread, and transport
settings, the verb must match, and the API key is re-read from
`$AGENTOS_API_KEY` because the value is never stored. Note that `--continue`
does not replay `--stream`, `--listen-port`, `--valkey-local-port`,
`--api-local-port`, or `--user`, so pass any of those again explicitly if the
original turn used a non-default value.

## Agent-facing output contract

The CLI's primary consumer is a coding agent (ADR-0021), so its output and
control flow are machine-first.

**`--json`** (global) makes every agent-facing verb emit a single
machine-readable JSON object on **stdout** instead of empty output: the
read/query verbs (`versions`, `memory`, `approvals`, `observability`), the
lifecycle result verbs (`kill`, `resume`, `budget`, `delete`), and every verb's
`--dry-run` plan (uniform shape `{"dry_run": true, "plan": [<lines>]}`) all
route through one centralized emitter. The `message` verbs keep their own,
more specific shapes: `agentos local message` and `agentos cluster message`
emit one structured line per terminal state on stdout -- a completed turn
emits `{"reply": ..., "thread": ..., "finalized": ...}` (the model's reply,
which is null on a no-edit completion, plus the thread the turn ran under); a
**timeout** emits `{"reply": null, "finalized": false, "timed_out": true}`
before exiting 3 (transient); and `--json --dry-run` emits a planned-action
descriptor `{"dry_run": true, "target": "local"|"cluster", "stream": ...,
"channel": ..., "reply_endpoint": ...}` (`channel` is null when it would be
resolved from the sole deployed agent). The three shapes are the `oneOf` in
`cli/schema/message.schema.json`. Two verbs lag this contract on their
real-path success output: `agentos skill message`, and the operator verbs
(`up`, `down`, `status`, `comms`) plus `deploy`, still print human text rather
than JSON on success (tracked in #485). All human and log text (progress,
notes, warnings) goes to **stderr**, so a plain `... --json | jq` yields clean
data. On failure under `--json`, the error is emitted to stdout as
`{"error": "<message>", "fix": "<hint>"|null}` instead of a prose message, so
an agent can recover without parsing prose. `NO_COLOR`, `CLICOLOR`, and
`--color=never` are honored on every command.

**Semantic exit codes** let an agent branch on *why* a command failed without
parsing output:

| Code | Class     | Meaning                                                                 |
|------|-----------|-------------------------------------------------------------------------|
| 0    | success   | The command did what was asked.                                         |
| 1    | failure   | A genuine runtime failure (well-formed request, operation did not succeed). Do not retry blindly. |
| 2    | usage     | A deterministic input error (missing `--yes`, a malformed flag/value, no bundle). Retrying the same argv fails identically -- fix the input. |
| 3    | transient | A retryable condition (the endpoint was unreachable or timed out). The same argv may succeed once the dependency is up. |
| 4    | unsupported | The verb was understood, but the concept it inspects does not exist at this tier by construction (`agentos skill versions`, `agentos skill memory`). No input and no retry changes that -- the same argv never succeeds here; the `fix` hint names the tier that does answer it. |

**Non-interactive by default.** Every mutating command has a non-interactive
path (`--yes` on `cluster down`/`kill`/`delete` and `local down --wipe`); none
block on stdin. A confirmation prompt that would otherwise read stdin refuses
with a usage error (exit 2) when the session is not a terminal, rather than
hanging.

(`agentos local status` and `agentos cluster status` proxy raw
`docker compose`/`helm`/`kubectl` output and do not yet support `--json`; use
`agentos skill status` for a machine-readable runner status today.)

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
