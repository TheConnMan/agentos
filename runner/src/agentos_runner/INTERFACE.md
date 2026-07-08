# INTERFACE: model-provider base-URL override seam

The seam that lets the runner target any Anthropic-compatible endpoint without
an Anthropic credential. Implemented in `sdk_auth.resolve_base_url_override`.

## Purpose

A generic, provider-agnostic base-URL override. When configured, the runner
points the claude-agent-sdk (and the bundled `claude` CLI it spawns) at a local
or third-party endpoint instead of real Anthropic, using a no-op placeholder in
place of a credential. Local Ollama is the FIRST implementation (issue #46).
OpenRouter (issue #24) is the NEXT consumer: it reuses this seam rather than
forking a parallel path. This is one seam, many providers.

## Env contract

### `ANTHROPIC_BASE_URL` (input)

When set to a non-empty value, the runner enters override mode. Unset or empty
means normal Anthropic credential resolution (`resolve_model_credential`).

### `ANTHROPIC_API_KEY` (output, set by the runner)

A NON-EMPTY no-op placeholder (`not-needed`, the `NO_OP_API_KEY` constant).

Empirical gotcha (verified 2026-07-07): the bundled Claude CLI treats an EMPTY
`ANTHROPIC_API_KEY` as "not logged in" and refuses to call the endpoint before
it ever dials the base URL. The placeholder MUST therefore be non-empty to pass
the CLI auth gate. It is deliberately not `sk-...` shaped so it can never be
mistaken for a real credential, and paired with the overridden base URL it
cannot authenticate against real Anthropic.

### `CLAUDE_CODE_OAUTH_TOKEN` (output, set by the runner)

Blanked to `""` in override mode so an inherited OAuth token cannot take
precedence over the placeholder + overridden base URL. This keeps override mode
hermetic.

### `AGENTOS_MODEL` (input)

The model name (e.g. `qwen3:4b`), passed through to `ClaudeAgentOptions.model`.

## How it arrives

- Docker/compose path: the worker injects it per-claim, mapping
  `AGENTOS_MODEL_BASE_URL` to `ANTHROPIC_BASE_URL` and `AGENTOS_MODEL` on the
  runner boot env (`apps/worker/src/agentos_worker/binding.py`).
- Cluster path: the chart bakes it into the SandboxTemplate when
  `inference.deploy=true` (`charts/agentos/templates/agent-sandbox.yaml`).

## Boundary

Documentation and one env-mapping function only. No speculative multi-provider
abstraction: that arrives with issue #24's second path. Tracked in
`.projects/prototype-review/seam-points-catalog.md`.
