# AgentOS Roadmap

Forward-looking work after the v0.1 MVP. The MVP loop is built and verified end
to end (see [`ARCHITECTURE.md`](../ARCHITECTURE.md)); this is what comes next,
roughly in priority order. Items are grouped by theme; within a group they are
ordered by leverage.

## 1. Retire the mock/demo surface — real API everywhere

The product is real; the UI should default to it. This is the headline item.

- Make the wired experience the default open, not the `acme-corp` fixture dataset behind `?api=1`. Remove `VITE_WIRED`/`?api=1` as a mode switch once the real API is the only path.
- **Delete the simulated CLI-terminal view entirely** ([`apps/ui/src/views/Terminal.tsx`](../apps/ui/src/views/Terminal.tsx)). A real `agentos` CLI exists ([`cli/`](../cli)); the scripted in-browser terminal is a demo prop, not a feature to wire up. Remove it rather than pointing it at real status.
- Wire the remaining `ComingSoon` placeholders to real backends: Evals matrix (the API `GET /evals/matrix` endpoint already exists and is unconsumed), Versions, Usage, Settings ([`apps/ui/src/views/wired/WiredStubs.tsx`](../apps/ui/src/views/wired/WiredStubs.tsx)).
- Delete the fixture stores and the demo dataset ([`apps/ui/src/fixtures/`](../apps/ui/src/fixtures/)) and the `acme-corp` literals in the chrome components once nothing imports them.

## 2. Thin-shim / contract debt

The thin-shim thesis holds, but three seams are hand-mirrored rather than frozen.

- **Queue payload.** The turn payload is a hand-mirrored dict on both sides ([`apps/dispatcher/src/agentos_dispatcher/queue.py`](../apps/dispatcher/src/agentos_dispatcher/queue.py) vs [`cli/src/queue.rs`](../cli/src/queue.rs)). Promote it into `packages/aci-protocol`, or add a golden-fixture compat test both sides run.
- **Eval-case format.** Two formats coexist: the CLI-local `cases.json` (`[{name, input, expect_contains}]`) and the platform bundle eval loader. Converge on one schema, ideally frozen alongside the plugin format, and migrate the `agentos init` template and the worker loader together.
- **Stream-consumer duplication.** The runs consumer and the eval consumer duplicate the Valkey `XREADGROUP`/consumer-group mechanics ([`apps/worker/src/agentos_worker/consumer.py`](../apps/worker/src/agentos_worker/consumer.py) vs [`apps/worker/src/agentos_worker/eval/stream.py`](../apps/worker/src/agentos_worker/eval/stream.py)). Extract one shared helper.

## 3. Observability and product features

- **Surface sandbox identity in the UI.** The runner now stamps `agentos.sandbox_id` on its trace resource ([`runner/src/agentos_runner/otel.py`](../runner/src/agentos_runner/otel.py)), so a trace already carries which sandbox served it. The remaining work is UI-side: show that sandbox in the run detail view, then add a per-run log proxy (k8s pod-logs API on cluster, `docker logs` locally) from that view.
- **Live sandbox list.** A dropdown or list view enumerating live sandboxes per agent (local: `docker ps`; k8s: `SandboxClaim`s).
- **Cold-boot latency.** The first Docker claim can exceed the bind window and force a kernel retry. Tune the bind timeout against image warm-up, consider a local warm pool / pre-pulled image, and surface a "booting runner" state in the Slack placeholder.
- **Empty-trace UX.** Stop writing observation-less trace shells for 0/0 eval replays; keep the honest empty state.
- **Dev/prod environment switcher.** Deployments already carry an environment (`dev`|`prod`) in the data model ([`apps/api/src/agentos_api/models.py`](../apps/api/src/agentos_api/models.py), `Environment` enum on `Deployment`) and in the CLI ([`cli/src/api.rs`](../cli/src/api.rs)), but the wired UI never surfaces it: a DEV/PROD pill exists only in the fixture chrome ([`apps/ui/src/components/Topbar.tsx`](../apps/ui/src/components/Topbar.tsx)) and is not wired to real env-scoped data. Build a real environment switcher that scopes agents, deployments, and observability, plus a promote-to-prod flow (the API git-flow promote path already exists; the UI has no button for it).
- **Fix the `latency_p95_seconds` field misnomer.** The Langfuse metrics field is named `latency_p95_seconds` ([`apps/api/src/agentos_api/metrics.py`](../apps/api/src/agentos_api/metrics.py)) but actually carries milliseconds; any consumer reading it as seconds is off by 1000x. Rename the field and its schema to the real unit, or convert at the boundary.
- **Per-thread reply routing across ingress paths.** `agentos message` (the no-Slack cluster driver) and a connected real Slack workspace both finalize a turn at a Slack API base URL, so they cannot yet safely coexist on one deployment. Route each turn's reply back to the ingress that enqueued it.
- **Transcript persistence across pod restarts.** Suspend/resume rehydrates from `AGENTOS_HISTORY_REF` (ADR-0003), but an unplanned runner-pod restart mid-thread loses the in-pod transcript. Persist enough turn history that a restarted sandbox rehydrates the live thread, not just an explicitly resumed one.
- **Slack Block Kit + no-edit streaming.** Replies are plain mrkdwn edited in place on one placeholder message ([`apps/worker/src/agentos_worker/mrkdwn.py`](../apps/worker/src/agentos_worker/mrkdwn.py)); richer Block Kit formatting and a streaming mode that avoids the repeated `chat.update` edits are open enhancements.

## 4. API / platform hygiene

- **Redeploy channel change.** `PATCH /agents/{id}` plus `agentos deploy --slack-channel` on redeploy, and a UI surface to change an agent's channel.
- **Bundle-files read endpoint** so the UI can show a bundle's tree beyond `SKILL.md`.
- **Eval backlog policy.** The eval consumer group reads from `0`; ancient test entries can spam errors on worker boot. Add a max-age or explicit requeue policy.
- **Graceful eval reporting.** `POST /evals/report` should return a clean 4xx on an unknown repo rather than erroring.
- **Configurable organization/workspace name.** `acme-corp` is fixture-only branding ([`apps/ui/src/components/`](../apps/ui/src/components/) chrome); the wired UI and API have no organization-name concept at all. A deployed install should present the operator's real company name: add an org/workspace name as an API setting or a chart value that feeds the UI chrome (topbar, sidebar, settings), so a fresh install is not branded with a demo company.
- **Boot hygiene.** Lazy-init the cluster/AWS client so a cold API boot does not emit a scary ERROR.
- **Hermetic worker tests.** The binding tests currently depend on a CI-step-migrated shared DB; make them self-contained.
- **`.gitignore` secret patterns.** Anchor `*credentials*` / `*secret*` patterns to real secret files; bare substrings silently swallowed source files during the build (`credentials.py` was renamed to `sdk_auth.py` to dodge it).

## 5. Providers and models

- **Non-Anthropic providers** via a LiteLLM Anthropic-format gateway: recognize an `sk-or-` (OpenRouter) prefix, point `ANTHROPIC_BASE_URL` at the bridge, and offer a values-gated LiteLLM sidecar in the chart. The runner mapping already fails loudly on foreign prefixes ([`runner/src/agentos_runner/sdk_auth.py`](../runner/src/agentos_runner/sdk_auth.py)), naming exactly what is supported, so this is additive.
- **Per-agent model selection** surfaced in the UI/manifest (`AGENTOS_MODEL` already exists in the boot-env path).

## 6. Release and developer experience

- **Cut v0.1.0** at MVP acceptance: tag, GitHub Release with CLI binaries and version-pinned images, then point the README install section at the release assets.
- **musl fully-static Linux CLI build** if glibc portability bites.
- **UI edit-agent surface:** view/edit skills and deploy a new version from the UI; then bundle file tree beyond `SKILL.md` and version history/rollback.
- **k8scratch standing deployment:** a CLI-vs-cluster runbook, and a decision on whether Slack serving moves in-cluster (one Socket Mode owner at a time) with a documented cutover.

## 7. Verification debt

- **Cold-start rehearsal** as the acceptance gate: timed, README-only, fresh clone to `helm install` to UI to an agent answering in Slack.
- **N1 soak on k8scratch:** chart resilience under sustained load — concurrent threads, mid-thread batch job, sandbox-kill-mid-run, resume-rehydrate. The harness is scaffolded at [`tests/soak`](../tests/soak); the scenario is not yet written.
- **Regression tests from live findings** where cheap: e.g. a worker boot warning when the substrate is Docker and no OTLP endpoint is set.

## 8. Cluster bring-up findings

From the first install of the chart on a fresh k3s cluster using the public
GHCR-default images (the first such install since the crashloop fixes). The two
chart fixes below are already merged to main (commit 7785999); the remaining
items are operator-facing notes and follow-ups.

- **DNS default broke in-cluster bundle fetch (fixed).** The agent-sandbox controller injects a public-resolver DNS default (8.8.8.8 / 1.1.1.1) when a `SandboxTemplate` leaves `dnsPolicy` unset, so bound sandbox pods could not resolve the in-cluster MinIO Service and bundle-fetch failed (the worker surfaced it as "claim not bound within 30s"). Fixed with a values-gated `agentSandbox.runner.dnsPolicy`, defaulting to `ClusterFirst`.
- **No-Slack middle-mode deploy needed a values knob (fixed).** Added a values-gated `worker.extraEnv` so a Slack-free middle-mode deploy can point `SLACK_API_BASE_URL` at an external CLI stub without editing templates.
- **Controller is opt-in; first install must enable it.** The chart ships the agent-sandbox CRDs, but the vendored controller is gated behind `agentSandbox.controller.deploy`. A cluster that has the CRDs but no controller silently never binds claims. A first install must set `agentSandbox.controller.deploy=true` unless the cluster already runs the controller. Worth a preflight or a louder default.
- **gVisor stays off without runsc on the node.** Use the `values-e2e-nogvisor` overlay on nodes without `runsc`. All other A2 security rails were verified ON in this install: default-deny egress, metadata-endpoint block, read-only rootfs, non-root, and per-agent secret isolation.
- **langfuse-web restarts ~2x during first boot** while ClickHouse and Postgres come up, then stabilizes. This is startup ordering, not a crashloop; do not treat the early restarts as a failure.
