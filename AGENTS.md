# AGENTS.md - AgentOS

Agent instructions for this repo. Start with [`README.md`](README.md) for what
the product is and how to run it, and [`ARCHITECTURE.md`](ARCHITECTURE.md) for
the component diagram, the message-flow sequence (Slack mention -> dispatcher ->
worker -> sandbox -> runner -> Slack reply), and the deploy-flow sequence (git
push -> webhook -> bundle pipeline -> deployment). The one-line version: a Slack
message is answered by a versioned plugin running in an isolated Kubernetes
sandbox, traced through Langfuse and steerable mid-turn; a git push deploys that
plugin under a bot identity via the API's git-flow engine. Relay is the project
codename; `agentos` is the product-surface name (bot handle, CLI binary). Read
`ARCHITECTURE.md` before touching a cross-component seam. If you are a coding
agent orienting in this repo, [`llms.txt`](llms.txt) is the curated machine map
of these docs, organized around the parity ladder. The two questions this repo
exists to answer are
[why did my agent work locally but break once deployed?](README.md#why-did-my-agent-work-locally-but-break-once-deployed)
and
[how do I test an agent the same way locally and on Kubernetes?](README.md#how-do-i-test-an-agent-the-same-way-locally-and-on-kubernetes) —
the same immutable bundle and the same `evals/cases.json` across `skill`,
`local`, and `cluster` is the answer to both.

## Directory map

One directory is one ownership boundary. Each area's own `CLAUDE.md` (linked
below) carries the rules and verify commands specific to that area -- read it
before editing there, in addition to this file.

| Path | Language | Scoped rules |
|---|---|---|
| `packages/aci-protocol` | Python (Pydantic + codegen) | [`packages/CLAUDE.md`](packages/CLAUDE.md) |
| `packages/plugin-format` | Python (Pydantic + codegen) | [`packages/CLAUDE.md`](packages/CLAUDE.md) |
| `apps/api` | Python (FastAPI) | [`apps/api/CLAUDE.md`](apps/api/CLAUDE.md) |
| `apps/dispatcher` | Python (Slack Bolt) | [`apps/dispatcher/CLAUDE.md`](apps/dispatcher/CLAUDE.md) |
| `apps/worker` | Python (redis-py) | [`apps/worker/CLAUDE.md`](apps/worker/CLAUDE.md) |
| `runner` | Python (claude-agent-sdk) | [`runner/CLAUDE.md`](runner/CLAUDE.md) |
| `apps/ui` | React (Vite + TS) | [`apps/ui/CLAUDE.md`](apps/ui/CLAUDE.md) |
| `cli` | Rust (clap + tokio) | [`cli/CLAUDE.md`](cli/CLAUDE.md) |
| `charts/agentos` | Helm | [`charts/agentos/CLAUDE.md`](charts/agentos/CLAUDE.md) |
| `tests/soak` | Python | -- |

The Python packages are one **uv workspace** (root `pyproject.toml`); ruff,
mypy, and pytest are configured at the root and run across all members.

## Verify commands (per package)

Run these from the repo root unless noted. CI (`.github/workflows/ci.yaml`) runs
the same commands.

**Python (all packages, from root):**
```bash
uv sync                 # once, and after any dependency change
uv run pytest -q        # all workspace tests
uv run ruff check .     # lint (auto-fix: uv run ruff check --fix .)
uv run mypy             # type-check (strict; targets the src dirs)
```

**Rust CLI:**
```bash
cd cli
cargo fmt --check
cargo clippy -- -D warnings
cargo test
```
If `cargo fmt`/`clippy` report a missing component: `rustup component add rustfmt clippy`.

**UI:** `cd apps/ui && pnpm install && pnpm lint && pnpm typecheck && pnpm test && pnpm e2e`.
The app is a real Vite + React + TS project -- see `apps/ui/CLAUDE.md`. The
top-level CI workflow's `ui` job runs the full pnpm lint, vitest, build, and
stackless Playwright suite; run `pnpm test`/`pnpm e2e` locally to match it.

Test discipline: test-first for behavior-bearing code; mock ONLY external
services (Slack, Anthropic, GitHub); NEVER mock Postgres/Valkey/Langfuse -- run
integration tests against the dev stack below. A change that only makes tests
pass by weakening assertions is a regression.

## The dev stack: compose.dev.yaml

The compose stack now has two profiles. `full` brings up the whole backing
stack (Postgres + Valkey + Langfuse v3 + ClickHouse + MinIO + OTel Collector).
`core` brings up the smaller local product loop (Postgres + Valkey + MinIO +
API + worker). Every backend integration test and UI E2E runs against `full`.

```bash
docker compose --profile full -f compose.dev.yaml up -d   # full stack
docker compose --profile core -f compose.dev.yaml up -d   # 7-service minimal stack (no Langfuse/ClickHouse/OTel/UI)
docker compose -f compose.dev.yaml ps        # check health
docker compose -f compose.dev.yaml down      # stop, KEEP volumes (fast restart)
docker compose -f compose.dev.yaml down -v   # stop and WIPE volumes (throwaway)
```

**Clean up after yourself — this is not optional.** If you bring the local
stack up, you MUST take it down when you are done. This box does not have the
RAM to leave the full stack idling, and this keeps happening: stacks get left
running across sessions and starve the machine. Before you end a session in
which you ran `agentos local up` / `docker compose ... up`, run `agentos local
down` (or `docker compose -f compose.dev.yaml down`) and confirm with
`docker ps` that nothing agentos-related is still up. Also remove any stray
`agentos-runner*` containers a run may have spawned. The thread that brought the
stack up owns tearing it down — a blocked or crashed test agent never cleans up
after itself, so do not assume someone else will.

**Do stack testing from a worktree, not the main checkout.** If a local test
requires code edits, make them in a git worktree cut from `origin/main` and land
them as a PR — never edit `main` in place to make a local run work. Read-only
runs against the current tree are fine; the moment you need to change code, cut a
worktree.

Add `--profile slack` through `agentos local up --slack` to start the optional dispatcher for real Slack.

Host ports (non-default host ports to avoid local collisions):

| Service | Host port |
|---|---|
| Langfuse UI | http://localhost:23000 |
| Postgres | localhost:25432 |
| Valkey | localhost:26379 |
| ClickHouse | HTTP 28123, native 29009 |
| MinIO | S3 29000, console 29001 |
| OTel Collector | gRPC 24317, HTTP 24318 |

Config lives in `.env.example` (copy to the gitignored `.env` to override; the
stack runs on the baked defaults without one). Load-bearing facts:

- **ClickHouse is pinned to `:24.8`.** Newer ClickHouse requires AVX and SIGILLs
  with exit 132 on CPUs without it. Keep the pin unless every target CPU has AVX.
  `charts/agentos` turns this into a chart preflight (`preflights.avxCheck`).
- **Langfuse OTLP ingest is HTTP-only** (gRPC is silently unsupported). Services
  may emit OTLP over gRPC or HTTP to the OTel Collector (4317/4318); the
  collector always exports to Langfuse over HTTP. Send app traces to the
  collector, not directly to Langfuse.
- **Langfuse is bootstrapped headless** with a fixed dev project (`agentos-dev`)
  and keys `pk-lf-agentos-dev` / `sk-lf-agentos-dev`, so the OTel path
  authenticates on first boot with no manual key-minting. Read traces back via
  `curl -u pk-lf-agentos-dev:sk-lf-agentos-dev http://localhost:23000/api/public/...`.

## Frozen contracts: STOP and escalate

`packages/aci-protocol` (the ACI session protocol + NDJSON events) and
`packages/plugin-format` (the Claude Code plugin shape, verbatim) are **frozen
interfaces**. Every lane compiles against them across three languages (Pydantic
source of truth -> committed JSON Schema -> generated TS + Rust), and the
schema-compat CI test fails any non-backwards-compatible change.

If your task needs a change to either package: **stop, do not work around it, and
open a GitHub issue or raise it in your PR** -- a contract change must land as
its own reviewed, backward-compatible change first, before dependent lanes
proceed. This also applies whenever an adopted component (Langfuse, Agent
Sandbox, Bolt) cannot do what a spec claims: stop and raise it with the evidence
rather than silently diverging.

## E2E verification is mandatory

Almost everything here is end-to-end testable, and the CLI makes it cheap: local
skills, the compose dev stack, and a disposable local k8s cluster (kind/k3s) let
you exercise a change against the real product loop, not a mock. So every
behavior-bearing change must be verified end-to-end before it is called done --
drive the actual surface (the `agentos` CLI, the deployed compose services, a
real sandbox on-cluster) with realistic input and assert the real outcome, not
just that unit tests pass.

- **In-repo tests are the durable net.** Prefer landing unit + integration tests
  (and a Playwright/e2e assertion where a UI or full-flow path changed) in the
  same PR. These are what keep the change working after you leave.
- **A hands-on e2e pass is non-negotiable on top of that.** Even when CI is
  green, run the changed path yourself through the CLI / docker / cluster and
  confirm the observable behavior. CI runs against frozen fixtures; a live pass
  catches config drift, deploy-pipeline regressions, and "is my code path even
  wired" gaps that unit tests cannot see.
- **Assert outcomes, not presence.** Use strong, deterministic assertions on real
  behavior (values, state transitions, emitted events, trace contents). Avoid
  hollow "does it render / does an element exist" checks and any AI-vision or
  screenshot-polling assertions -- they mask weak architecture and rot fast.
- **New/changed CLI commands follow the agent-facing contract (ADR-0021):**
  structured `--json` output for read/report commands (JSON to stdout, human/log
  to stderr), semantic exit codes (0 success / 1 failure / 2 usage / 3 transient),
  non-interactive (a `--yes`/`--force` path, never blocking on stdin), and errors
  as `{"error","fix"}` recovery instructions. Exit-code scheme: see
  `cli/README.md`.
- **Console/CLI parity is a two-sided invariant (epic #145):** any CLI
  command-surface change regenerates the committed manifest (`cli/CLAUDE.md`),
  and every wired console action maps to a real command or an explicit
  `noCliEquivalent` (`apps/ui/CLAUDE.md`). Keep both sides in the same change.

## Playwright: two modes

- **The merge gate is the committed E2E suite** under `apps/ui` (Playwright,
  headless, in CI against the compose stack). It asserts behavior (deploy flow
  completes, runs view renders the tool-call tree, eval matrix populates). This
  is the regression net; it must be green to merge.
- **The `@playwright/mcp` server** (wired in `.mcp.json`) is for interactive
  verification *during* development: drive the real browser, click through the
  flow you just built, and screenshot it to check visual fidelity. Commit
  assertions into the suite to make them a gate.

## Cluster verification

Chart, sandbox, and soak verification need a real cluster; a disposable local
`kind` or `k3s` cluster works. See [`charts/agentos/CLAUDE.md`](charts/agentos/CLAUDE.md)
for the install and probe commands.

## Branch and commit conventions

- Branch per change: `task/<short-description>`, cut from the latest
  `origin/main`. Never commit to `main`.
- Commit message format: a short imperative summary line, then detail bullets.
- Reference the relevant issue in the PR body (e.g. `Closes #123`).
- **Never mention any AI assistant (Claude, Codex, GPT, etc.) or AI in general in
  commit messages, and never add `Co-Authored-By` lines referencing AI.**
- No dashes/emdashes in prose content; no emojis in code or docs.

## Decisions: ADR vs. GitHub issue

Two different tools; do not conflate them.

- Write an **ADR** (`docs/adr/`, see ADR-0001) only for a **cross-cutting
  architectural decision that closes the door on alternatives.** It is a choice
  about the *shape* of the system (a contract, a seam, a substrate, an invariant)
  that is expensive to reverse and whose *why* a future contributor must understand
  before touching that area. An ADR is not just what we chose; it **must record what
  we decided against and why** (the alternatives and their rejection). If no real
  alternative is being closed off, it is not an ADR.
- Write a **GitHub issue** (with a rich description) for a **feature**, however
  large. A new CLI command, a UI surface, a connector: it may be a lot of code, but
  it is deletable and does not change the architecture, so it is a feature, not an
  architectural decision. The issue carries the what and the why; the *how* lives in
  the PR. An issue may cite an ADR.
- **When in doubt, write the issue.** Promote to an ADR only when the same decision
  gets re-explained across a third issue or PR.

## Gotchas discovered during the build

- **Deployment-to-runtime binding is wired; it binds per fresh mention.** The
  worker resolves a thread's Slack channel to its agent, that agent's active
  deployment (prod outranks dev, then most recent), and the resolved
  `AGENTOS_BUNDLE_REF`, injecting it into each sandbox claim so a fresh mention
  boots the exact bundle version the API's git-flow engine produced
  (`apps/worker/src/agentos_worker/binding.py`). The seam to remember: an
  existing thread keeps the sandbox and bundle it first booted with; only a new
  mention (a new claim) picks up a newer deployment.
- **Sandbox cold boots must never pull an image.** The four Deployment services
  (`api`, `worker`, `dispatcher`, `ui`) default to `pullPolicy: Always`, but the
  runner image is `IfNotPresent` and kept pinned on every node by the
  `agentSandbox.runner.prewarm` DaemonSet, which pulls at install/upgrade. A
  mid-boot pull of the ~380MB runner image blew the 90s claim timeout in a live
  incident; never switch the runner image to `Always`
  (`charts/agentos/templates/runner-prewarm.yaml`, `charts/agentos/values.yaml`).
- **Suspend/resume is a cold rehydrate, not a live hibernate** (ADR-0003). A
  suspended sandbox's pod is deleted; resume creates a new pod and injects
  `AGENTOS_HISTORY_REF`. Never assume prompt-cache warmth survives a suspend, and
  never design a feature that needs a sandbox's in-process state to outlive a
  suspend.
- **Warm-pool claims are fast only without per-claim env.** A claim that needs
  `AGENTOS_HISTORY_REF`/`AGENTOS_SESSION_ID` injected (the resume path) cannot
  bind a pre-warmed sandbox and cold-creates one instead (seconds, not the ~0.2s
  warm-pool bind). This is inherent to `agent-sandbox`'s
  `envVarsInjectionPolicy: Overrides`, not a bug to fix.
- **A cluster's CNI must actually enforce NetworkPolicy** or the chart's egress
  lockdown is a silent false-pass. The chart ships a before/after enforcement
  probe (`preflights.networkPolicyProbe`) for exactly this reason -- never trust
  an egress policy without it.
- **gVisor needs `runsc` on the node**, which the chart cannot install. On a
  cluster without it, use the ready-made `-f charts/agentos/values-e2e-nogvisor.yaml`
  overlay rather than hand-editing security values (see `charts/agentos/CLAUDE.md`).
