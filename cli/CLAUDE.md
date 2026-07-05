# CLAUDE.md — cli

The `agentos` CLI (task I1): Rust, clap + tokio + reqwest. Speaks only the
frozen contracts (the generated `agentos-aci-protocol` crate over HTTP/NDJSON,
and the platform API's committed `apps/api/openapi.json`) and orchestrates a
local runner container via Docker. Full command reference in `cli/README.md`.

## Load-bearing invariants

- **Never hand-write the ACI types.** `Cargo.toml` depends on
  `agentos-aci-protocol` at `../packages/aci-protocol/generated/rust` --
  that crate is generated from the frozen Pydantic models. If a type you
  need is missing, that is a contract gap (escalate per the root CLAUDE.md
  frozen-contracts rule), not something to redefine locally in `cli/src`.
- **No new HTTP crates.** `reqwest` (rustls-tls, no OpenSSL) is the one HTTP
  client in this binary; do not add a second one for a specific endpoint.
  Keep `reqwest`'s feature set minimal (`json`, `stream`, `multipart`) --
  adding a feature should come with a reason in the PR, not just convenience.
- **The queue seam is mirrored, not imported, across languages.** The CLI
  never talks to the dispatcher's Valkey Stream directly -- `agentos send`
  talks straight to a local runner container's ACI HTTP surface
  (`/v1/event`, `/v1/steer`, `/v1/interrupt`), bypassing the
  dispatcher/worker entirely by design (that is the point: zero Slack, zero
  cluster). If a future task wires the CLI to the real dispatcher/worker
  queue seam (the planned `agentos chat` middle mode), mirror the
  `QueuedSlackEvent` shape rather than importing Python types into Rust --
  keep the contract-mirroring discipline explicit at the boundary.
- **`agentos init` scaffolds the plugin-format shape verbatim.** The
  generated bundle (`.claude-plugin/plugin.json`, `skills/<name>/SKILL.md`,
  `.mcp.json`) must stay byte-compatible with what `plugin_format.validate_bundle`
  accepts -- if `packages/plugin-format` changes, this scaffold needs
  updating in the same reviewed change, not independently.
- **`start` records container state in `.agentos/runner.json`** (gitignored
  by the scaffold) so `send`/`eval`/`status`/`stop` can resolve the running
  container from the bundle directory alone. Do not add a second
  state-tracking file for the same purpose.
- **`deploy` is the one command that leaves the local machine.** It packages
  the bundle as tar.gz and pushes to the platform API
  (find-or-create agent, create version, upload bundle, create deployment)
  authenticated via `--api-key`/`AGENTOS_API_KEY`. Every other command
  (`init`, `start`, `send`, `eval`, `status`, `stop`) must keep working with
  zero network access beyond the local Docker daemon and the local runner
  container.

## Verify

```bash
cd cli && cargo fmt --check && cargo clippy -- -D warnings && cargo test
```

Scripted E2E (real runner container, fake model, fully offline):
```bash
bash cli/scripts/e2e.sh
```
With the compose stack + a local `apps/api` up, the same script also
exercises the deploy leg:
```bash
AGENTOS_E2E_NETWORK=agentos_default \
AGENTOS_E2E_OTEL=http://otel-collector:4318 \
AGENTOS_E2E_API_URL=http://localhost:8000 bash cli/scripts/e2e.sh
```
Both require the `agentos-runner` image built once from the repo root
(`docker build -f runner/Dockerfile -t agentos-runner .`).
