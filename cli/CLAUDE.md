# CLAUDE.md - cli

The `agentos` CLI: Rust, clap + tokio + reqwest. Speaks only the
frozen contracts (the generated `agentos-aci-protocol` crate over HTTP/NDJSON,
and the platform API's committed `apps/api/openapi.json`) and orchestrates a
local runner container via Docker. Three command families: `skill` drives a
plugin against a local runner with `up`, `down`, `status`, `message`, and
`eval`; `local` wraps the compose stack and local API with `up`, `down`,
`status`, `message`, and `deploy`; `cluster` wraps Helm and the deployed
release with `up`, `status`, `down`, `message`, and `deploy`. Full command reference in
`cli/README.md`.

## Load-bearing invariants

- **Never hand-write the ACI types.** `Cargo.toml` depends on
  `agentos-aci-protocol` at `../packages/aci-protocol/generated/rust` --
  that crate is generated from the frozen Pydantic models. If a type you
  need is missing, that is a contract gap (raise it in an issue/PR first per
  the root AGENTS.md frozen-contracts rule), not something to redefine locally
  in `cli/src`.
- **No new HTTP crates.** `reqwest` (rustls-tls, no OpenSSL) is the one HTTP
  client in this binary; do not add a second one for a specific endpoint.
  Keep `reqwest`'s feature set minimal (`json`, `stream`, `multipart`) --
  adding a feature should come with a reason in the PR, not just convenience.
- **The queue seam is mirrored, not imported, across languages.** The CLI
  never talks to the dispatcher's Valkey Stream directly -- `agentos skill message`
  talks straight to a local runner container's ACI HTTP surface
  (`/v1/event`, `/v1/steer`, `/v1/interrupt`), bypassing the
  dispatcher/worker entirely by design (that is the point: zero Slack, zero
  cluster). If a future task wires the CLI to the real dispatcher/worker
  queue seam (the future dispatcher or worker queue surface), mirror the
  `QueuedSlackEvent` shape rather than importing Python types into Rust --
  keep the contract-mirroring discipline explicit at the boundary.
- **`agentos init` scaffolds the plugin-format shape verbatim.** The
  generated bundle (`.claude-plugin/plugin.json`, `skills/<name>/SKILL.md`,
  `.mcp.json`) must stay byte-compatible with what `plugin_format.validate_bundle`
  accepts -- if `packages/plugin-format` changes, this scaffold needs
  updating in the same reviewed change, not independently.
- **`skill up` records container state in `.agentos/runner.json`** (gitignored
  by the scaffold) so `skill message`/`skill eval`/`skill status`/`skill down` can resolve the
  running container from the bundle directory alone. Do not add a second
  state-tracking file for the same purpose.
- **The local skill verbs stay fully offline; the local and cluster
  target verbs are the exception.** `init`, `skill up`, `skill down`,
  `skill status`, `skill message`, and `skill eval` must keep working with
  zero network access beyond the local Docker daemon and the local runner
  container. `deploy` is the one bundle shipping verb that leaves the machine: it packages the bundle as
  tar.gz and pushes to the platform API (find-or-create agent, create version,
  upload bundle, create deployment) authenticated via
  `--api-key`/`AGENTOS_API_KEY`. `local message`, `local deploy`,
  `cluster message`, `cluster deploy`, and every operator verb
  (`local up`, `local status`, `local down`, `cluster up`, `cluster status`, `cluster down`) reach a Valkey, API, or cluster by design.
- **The operator verbs are a thin wrapper; the chart stays the source of
  truth.** `cluster up`/`cluster status`/`cluster down` (`src/ops.rs`) and
  `local <up|down|status>` (`src/local.rs`) shell out to
  `helm`/`kubectl`/`docker compose` and never re-derive what a values file
  already declares. Each verb builds its command lines as a pure function
  returning `OpsCommand` vectors that the executor or the `--dry-run` printer
  consumes; keep that split so argv stays unit-testable with no cluster, and
  give any new verb a matching `--dry-run`.
- **Credentials are masked, never printed.** Secret `helm --set` values use the
  `CmdArg::SecretSet` variant (only a masked prefix is echoed, in dry-run or the
  printed command line) and token flags read from env with `hide_env_values`.
  Never widen a secret to `Plain` or otherwise print it. The `up` model
  credential (from `AGENTOS_MODEL_CREDENTIALS`) flows through this path. Slack is
  connected with a raw `helm upgrade --reuse-values` (setting the dispatcher
  tokens and clearing `worker.slackApiBaseUrl=` to un-wire any `message` stub
  routing), not a CLI verb; see the chart NOTES.
- **`cluster message` self-plumbs and guards against hijacking real Slack.** It manages
  its own kubectl port-forwards (children killed on exit) and, when wiring the
  deployed worker to its local stub, refuses if a `<release>-dispatcher`
  Deployment exists (a real workspace is connected) unless `--force-wire`. Do
  not drop that guard: the stub wiring is cluster-wide and would divert a live
  workspace's replies.

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
