# runner

The runner image and SDK adapter: the productized prototype,
a long-lived streaming session server that implements the full ACI
v0.1 contract from `packages/aci-protocol`. Built on `claude-agent-sdk` (Python).
Runs inside a claimed Agent Sandbox; the CLI (`agentos skill up`) also runs it
locally in Docker.

## What it does

- One long-lived `claude-agent-sdk` streaming-input session per process (one per
  sandbox) -- the source of prompt-cache affinity across turns.
- Accepts inbound ACI frames (`event` of type message | job | eval_case, and
  `interrupt`) and streams outbound NDJSON (`text_delta` | `tool_note` | `final`
  | `error` | `side_effect_flag`) with protocol-version enforcement.
- Enforces `AGENTOS_BUDGET.max_output_tokens_per_run` (halts a run with a
  classified-failure final) and hands the daily USD cap to the SDK natively.
- Emits `side_effect_flag` when a non-idempotent tool executes (read-only
  allowlist, deny-by-default; see `side_effects.py`).
- Loads and validates the mounted plugin bundle via `plugin_format.validate_bundle`.
- Exports gen_ai OTel spans (`agent.run` -> `llm.generation` -> `execute_tool`)
  OTLP-HTTP to the collector, which forwards to Langfuse.
- Rehydrates from a history ref on start (`resume`), stateless-first (ADR-0003).

## HTTP surface (ACI channel)

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | Liveness. |
| GET | `/status` | Session status (done / idle-awaiting-input / classified-failure), readiness, turn state. |
| POST | `/v1/event` | Open a turn: body is an ACI `event` frame; streams outbound NDJSON, ending in a `final`. |
| POST | `/v1/steer` | Inject a follow-up into the live turn (`{"text": ...}`); 409 when no turn is active. |
| POST | `/v1/interrupt` | Hard-stop the live turn: body is an ACI `interrupt` frame. |

One turn consumes the SDK generator at a time; steer and interrupt are
side-channel injections whose output surfaces on the open `/v1/event` stream (the
proven steering pattern). The finish race (a steer arriving as a turn ends,
409) is owned by the worker.

The three POST routes (`/v1/event`, `/v1/steer`, `/v1/interrupt`) require an
`Authorization: Bearer <token>` header matching `AGENTOS_RUNNER_TOKEN` when that
env var is set, returning 401 otherwise; this is per-sandbox transport auth
(defense-in-depth on the ACI ingress alongside the NetworkPolicy), not part of
the frozen ACI wire contract. Enforcement is only-when-configured: with the var
unset the app is pass-through (CLI, fake-model CI, and pre-token sandboxes stay
unauthenticated). `GET /healthz` and `GET /status` are never gated (the chart
readinessProbe hits `/healthz`).

## Environment

ACI-frozen (`aci-protocol.SessionConfig`): `AGENTOS_PLUGIN_DIR`,
`AGENTOS_SESSION_ID`, `AGENTOS_SANDBOX_ID`, `AGENTOS_BUDGET`, optional
`AGENTOS_MEMORY_REF` / `AGENTOS_CREDENTIALS`, `OTEL_EXPORTER_OTLP_*`.
Runner-local: `AGENTOS_MODEL`, `AGENTOS_SYSTEM_PROMPT`, `AGENTOS_MAX_TURNS`,
`AGENTOS_HISTORY_REF` (rehydrate; falls back to `AGENTOS_MEMORY_REF`),
`AGENTOS_IDEMPOTENT_TOOLS` (override the read-only allowlist),
`AGENTOS_RUNNER_PORT`, `AGENTOS_RUNNER_TOKEN` (per-sandbox bearer token gating the
three ACI POST routes; enforced only when set), `AGENTOS_FAKE_MODEL` (offline
smoke; no model call).

## OpenCode harness bundle fidelity

The optional OpenCode second harness (issue #25) does not consume a Claude plugin
bundle directly; `opencode/installer.py` compiles the validated bundle into an
OpenCode session workdir (`opencode.json` MCP config, `.claude/skills/`,
`.opencode/commands/`, `.opencode/agents/`). That compilation is copy-verbatim
where OpenCode's leniency permits and a field remap only where the formats
differ, so a few Claude-only elements are lost in translation (skill
`allowed-tools`, command/agent `model` aliases, agent `tools` lists, `scripts/`,
manifest hooks, non-`${CLAUDE_PLUGIN_ROOT}` `${VAR}` references, the http/sse
transport distinction). The **canonical per-element fidelity record** lives in the
`opencode/installer.py` module docstring; each loss also surfaces at runtime as a
`logger.warning` on `agentos_runner.opencode.installer`.

## Build and smoke

The image compiles against the frozen workspace packages, so build from the repo
root:

```bash
docker build -f runner/Dockerfile -t agentos-runner .
# Offline round-trip (fake model, no credential), OTel to the dev collector:
docker run -d --name runner-smoke --network agentos_default \
  -e AGENTOS_FAKE_MODEL=1 -e AGENTOS_PLUGIN_DIR=/unused \
  -e AGENTOS_SESSION_ID=smoke -e AGENTOS_SANDBOX_ID=sbx \
  -e 'AGENTOS_BUDGET={"max_output_tokens_per_run":100000,"max_usd_per_day":5.0}' \
  -e OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318 \
  -p 18080:8080 agentos-runner
curl -sN -X POST http://localhost:18080/v1/event -H 'Content-Type: application/json' \
  -d '{"kind":"event","type":"message","text":"hi","user":"U","ts":"1.0"}'
```

### OpenCode image variant

Build the separate OpenCode runner image from the repository root:

```bash
docker build -f runner/Dockerfile.opencode -t agentos-runner-opencode .
```

Run the offline fake smoke under the same filesystem and user rails as the
chart. This path does not require an OpenCode binary call or model credential:

```bash
docker run -d --name opencode-runner-smoke \
  --read-only --tmpfs /tmp --tmpfs /home/runner:uid=1000,gid=1000 \
  --user 1000:1000 --cap-drop ALL \
  -e AGENTOS_FAKE_MODEL=1 -e AGENTOS_PLUGIN_DIR=/unused \
  -e AGENTOS_SESSION_ID=smoke -e AGENTOS_SANDBOX_ID=sbx \
  -e 'AGENTOS_BUDGET={"max_output_tokens_per_run":100000,"max_usd_per_day":5.0}' \
  -p 18080:8080 agentos-runner-opencode
curl -s http://localhost:18080/healthz
curl -sN -X POST http://localhost:18080/v1/event \
  -H 'Content-Type: application/json' \
  -d '{"kind":"event","type":"message","text":"hi","user":"U","ts":"1.0"}'
docker rm -f opencode-runner-smoke
```

Run live conformance inside the container with an OpenRouter credential:

```bash
docker run --rm --read-only --tmpfs /tmp --tmpfs /home/runner:uid=1000,gid=1000 \
  --user 1000:1000 --cap-drop ALL \
  -e AGENTOS_CREDENTIALS="$OPENROUTER_API_KEY" \
  agentos-runner-opencode python -m agentos_runner.opencode.conformance
```

This image intentionally has no Node runtime. Node based stdio MCP servers in
plugin bundles cannot run in this variant; Python based servers can. Under this
image, `AGENTOS_MODEL` is passed verbatim as the OpenRouter modelID. Operators
must override the chart default `claude-sonnet-5`, or turns fail at the provider.
An `AGENTOS_HISTORY_REF` causes startup to fail because OpenCode has no resume
support. A positive daily USD cap logs a startup warning because OpenCode cannot
enforce that cap natively; the per run output token ceiling remains enforced.

## Verify (from repo root)

```bash
uv run pytest runner/tests -q   # unit + integration + conformance
uv run ruff check . && uv run mypy
```

Live tests (`runner/tests/test_live.py`) run only when `CLAUDE_CODE_OAUTH_TOKEN`
or `ANTHROPIC_API_KEY` is present; otherwise they are skipped.
