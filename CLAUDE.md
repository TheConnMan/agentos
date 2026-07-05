# CLAUDE.md — Relay (AgentOS)

Guidance for agents implementing tasks in this repo. Relay is the project codename; `agentos` is the product-surface name (bot handle, CLI binary). The build is a fleet of individually-verifiable background jobs along the DAG in `docs/build-orchestration-plan.md` §4; read that file and `docs/mvp-build-plan.md` before starting a task. Every task owns one directory boundary and must verify its own work with no human in the loop.

## Worktree protocol (mandatory for every task agent)

Concurrent agents share one `.git`; the primary checkout at the repo root is NOT yours. Before your first edit:

```bash
git -C /home/user/git/curietech/agentos worktree add \
  /home/user/git/curietech/agentos-<taskid> -b task/<taskid>-<desc> main
cd /home/user/git/curietech/agentos-<taskid> && uv sync
```

Work exclusively in your worktree. Never run `git checkout`, `commit`, or `add` in the primary checkout. Stage only paths you own (never `git add -A`). If you changed dependencies, regenerate `uv.lock` in your own worktree (`uv lock`). The orchestrator merges your branch into main and removes your worktree; do not merge or delete branches yourself.

## Monorepo layout and directory ownership

One task owns one directory; two agents never edit the same directory.

| Path | Language | Owning task(s) |
|---|---|---|
| `packages/aci-protocol` | Python (Pydantic + codegen) | C1 |
| `packages/plugin-format` | Python (Pydantic + codegen) | C1 |
| `apps/api` | Python (FastAPI) | B1, B2, J1, OB1 |
| `apps/dispatcher` | Python (Slack Bolt) | E1 |
| `apps/worker` | Python (redis-py) | F1, G1, K1 |
| `runner` | Python (claude-agent-sdk) | D1 |
| `apps/ui` | React (Vite + TS) | H1a, H1b, OB1 |
| `cli` | Rust (clap + tokio) | I1 |
| `charts/agentos` | Helm | A1, A2 |
| `tests/soak` | Python | N1 |

The Python packages are one **uv workspace** (root `pyproject.toml`); ruff, mypy, and pytest are configured at the root and run across all members.

## Verify commands (per package)

Run these from the repo root unless noted. CI (`.github/workflows/ci.yaml`) runs the same commands.

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

**UI (once H1a scaffolds the Vite app):** `cd apps/ui && pnpm install && pnpm lint && pnpm test && pnpm exec playwright test`. Until then the CI `ui` job is an echo-skip placeholder.

Test discipline (from the global AGENTS.md): test-first for behavior-bearing code; mock ONLY external services (Slack, Anthropic, GitHub); NEVER mock Postgres/Valkey/Langfuse — run integration tests against the dev stack below. A change that only makes tests pass by weakening assertions is a regression.

## The dev stack (verification tier V1): compose.dev.yaml

One command brings up the whole backing stack (Postgres + Valkey + Langfuse v3 + ClickHouse + MinIO + OTel Collector). Every backend integration test and UI E2E runs against it.

```bash
docker compose -f compose.dev.yaml up -d     # bring up (idempotent)
docker compose -f compose.dev.yaml ps        # check health
docker compose -f compose.dev.yaml down      # stop, KEEP volumes (fast restart)
docker compose -f compose.dev.yaml down -v   # stop and WIPE volumes (throwaway)
```

Host ports (chosen to avoid the CurieTech platform E2E stack, which uses 55432/55433/8000/3000/8080/9091):

| Service | Host port |
|---|---|
| Langfuse UI | http://localhost:3001 |
| Postgres | localhost:55434 |
| Valkey | localhost:56379 |
| ClickHouse | HTTP 8124, native 9011 |
| MinIO | S3 9002, console 9003 |
| OTel Collector | gRPC 4317, HTTP 4318 |

Config lives in `.env.example` (copy to the gitignored `.env` to override; the stack runs on the baked defaults without one). Load-bearing facts:

- **ClickHouse is pinned to `:24.8`.** Newer ClickHouse needs AVX and SIGILLs with exit 132 on CPUs without it. This host has only `sse4_2` (no AVX), so the pin is required here. A1 turns this into a chart preflight.
- **Langfuse OTLP ingest is HTTP-only** (gRPC is silently unsupported). Services may emit OTLP over gRPC or HTTP to the OTel Collector (4317/4318); the collector always exports to Langfuse over HTTP. Send app traces to the collector, not directly to Langfuse.
- **Langfuse is bootstrapped headless** with a fixed dev project (`agentos-dev`) and keys `pk-lf-agentos-dev` / `sk-lf-agentos-dev`, so the OTel path authenticates on first boot with no manual key-minting. Read traces back via `curl -u pk-lf-agentos-dev:sk-lf-agentos-dev http://localhost:3001/api/public/...`.

## Frozen contracts: STOP and escalate

`packages/aci-protocol` (the ACI session protocol + NDJSON events) and `packages/plugin-format` (the Claude Code plugin shape, verbatim) are **frozen interfaces**. Every lane compiles against them across three languages (Pydantic source of truth → committed JSON Schema → generated TS + Rust), and the schema-compat CI test fails any non-backwards-compatible change.

If your task needs a change to either package: **stop, do not work around it, and escalate to the orchestrator.** The orchestrator lands the contract change as its own reviewed PR before dependent lanes proceed. This also applies whenever an adopted component (Langfuse, Agent Sandbox, Bolt) cannot do what a spec claims: stop and escalate with the evidence rather than silently diverging.

## Playwright: two modes

- **The merge gate is the committed E2E suite** under `apps/ui` (Playwright, headless, in CI against the compose stack). It asserts behavior (deploy flow completes, runs view renders the tool-call tree, eval matrix populates). This is the regression net; it must be green to merge.
- **The `@playwright/mcp` server** (wired in `.mcp.json`) is for interactive verification *during* development: drive the real browser, click through the flow you just built, screenshot it, and diff against the corresponding `AgentOS.html` demo state (the design file renders locally). Use the MCP to check visual fidelity; commit assertions into the suite to make them a gate.

## k8scratch (verification tier V3)

A disposable scratch k3s cluster for chart/sandbox/soak verification (G1, A1/A2, N1).

```bash
export KUBECONFIG=~/k8scratch/.kube/k8scratch.yaml
kubectl get nodes
```

It is **disposable** (tear down what you create; leave it clean) and currently **4 GB / 4 cores**. Early cluster tasks (G1 sandbox lifecycle, A2 NetworkPolicy probes) fit in 4 GB with the observability stack left on the dev box. The full definition-of-done target is ~8-10 vCPU / ~20 GB; the walking-skeleton gate and N1 require a resize (a Brian action). Fallback for chart-only tests is a local `kind` cluster.

## Branch and commit conventions

- Branch per task: `task/<id>-<short-description>` (e.g. `task/c1-frozen-contracts`), cut from the latest `origin/main`. Never commit to `main`.
- Commit message format: a short imperative summary line, then detail bullets. Add `Ref CUR-xxx` only when the branch carries a real Linear ticket. PR body carries the closing magic word (`Closes CUR-xxx`).
- **Never mention any AI assistant (Claude, Codex, GPT, etc.) or AI in general in commit messages, and never add `Co-Authored-By` lines referencing AI.**
- No dashes/emdashes in prose content; no emojis in code or docs.
- Background `/implement` runs have standing approval to push their branch and open a PR against `main`; interactive runs wait for explicit approval.
