# 15. The credential plane: no broker, prefix-mapped, fail-loud and fail-closed

Date: 2026-07-09
Status: Accepted

Retroactive record of how a model credential reaches the model, already built into
[`runner/src/agentos_runner/sdk_auth.py`](../../runner/src/agentos_runner/sdk_auth.py).
ADR-0005 anticipated this ("production runners need a proper API-key / Bedrock /
Vertex path, ADR-worthy when that lane lands"); that lane has landed
([#24](https://github.com/curie-eng/agentos/issues/24)).

## Context

The runner holds a customer's model credential, the install will be
security-reviewed, and customers want to bring their own model (Anthropic direct,
OpenRouter, a bundled local model). Two things had to be true at once: the secret
must not sprawl across application processes, and a misconfigured or unusable
credential must fail visibly rather than silently degrade.

## Decision

A credential flows from a Helm Secret to the model env variable with no
application process brokering it: chart Secret -> worker env
`AGENTOS_CREDENTIALS` -> injected into the claim boot env -> the runner maps it.
The runner's mapping is prefix-based and fails loud on anything it cannot use
(`sdk_auth.py:39`, `:51`):

- `sk-ant-oat...` -> `CLAUDE_CODE_OAUTH_TOKEN` (checked first).
- `sk-ant-...` -> `ANTHROPIC_API_KEY`.
- `sk-or-...` (OpenRouter) -> the shared base-URL-override seam: base URL points at
  OpenRouter's native Anthropic Messages endpoint and the key goes in the
  `x-api-key` header, staying on the Anthropic wire format so prompt caching
  survives.
- bare `sk-...` -> raises `UnsupportedCredentialError` rather than forwarding a key
  the SDK cannot use.

The real model is the default. `AGENTOS_FAKE_MODEL` is a test-only knob; a missing
credential fails **closed** rather than degrading to the fake model
([`binding.py:137`](../../apps/worker/src/agentos_worker/binding.py),
[`sandbox/docker.py:18`](../../apps/worker/src/agentos_worker/sandbox/docker.py),
[`runner/__main__.py:62`](../../runner/src/agentos_runner/__main__.py)). An explicit
SDK credential already in the env always wins, and the mapping is a no-op when
`AGENTOS_CREDENTIALS` is unset.

## Alternatives considered

- **A credential broker / vault sidecar service.** Rejected: it adds a
  secret-handling process to audit and a runtime dependency, for a problem the
  chart Secret plus prefix-mapping already solves. Fewer processes touch the
  secret, the better it reviews.
- **Forward whatever credential is in the ambient environment (convenience
  fallback).** Rejected, and this is not hypothetical: an ambient
  `CLAUDE_CODE_OAUTH_TOKEN` shadowing `AGENTOS_CREDENTIALS` leaked into runners and
  broke auth ([#106](https://github.com/curie-eng/agentos/issues/106), fixed in
  [PR #109](https://github.com/curie-eng/agentos/pull/109)). Positive, by-name
  credential selection is the deliberate guard.
- **Default to the fake model when no credential is present.** Rejected: it turns
  a misconfiguration into a silently-wrong answer. Fail-closed surfaces the problem
  at deploy time.
- **A separate harness per model provider for BYO.** Rejected: OpenRouter exposes a
  native Anthropic Messages endpoint, so a base-URL override keeps one wire format
  and preserves prompt caching, instead of swapping the harness
  ([#24](https://github.com/curie-eng/agentos/issues/24); see PRs
  [#94](https://github.com/curie-eng/agentos/pull/94),
  [#98](https://github.com/curie-eng/agentos/pull/98),
  [#105](https://github.com/curie-eng/agentos/pull/105)).

## Consequences

- The fail-loud / fail-closed posture is load-bearing security behaviour: a future
  "just forward the key we have" or "fall back to fake" reopens the credential-leak
  and silent-wrong-model failure classes this ADR closes.
- The base-URL-override seam is the single extension point for any
  Anthropic-compatible endpoint (OpenRouter today, a bundled local model via
  `--local-model`); a new provider on the Anthropic wire format is config, not code.
- Per-agent connector and MCP-server secrets are a separate credential plane,
  recorded in ADR-0009; this ADR governs only the model credential the SDK reads.
- Related hardening (default credentials shipping enabled, the ACI HTTP channel
  having no auth) is tracked separately
  ([#57](https://github.com/curie-eng/agentos/issues/57),
  [#63](https://github.com/curie-eng/agentos/issues/63)) and does not change this
  decision.
