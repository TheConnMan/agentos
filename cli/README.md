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
| `agentos status` / `agentos stop` | Session status / tear down the container. |
| `agentos deploy` | Package the bundle as tar.gz and push it to the platform API (find-or-create agent, create version, upload bundle, create deployment). Auth via `--api-key` / `AGENTOS_API_KEY`. |

`start` records the container in the bundle's `.agentos/runner.json`
(gitignored by the scaffold); `send`/`eval`/`status`/`stop` run from the bundle
directory and resolve the runner from it, or accept `--url`.

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
