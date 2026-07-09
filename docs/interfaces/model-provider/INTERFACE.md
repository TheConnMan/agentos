# INTERFACE: Model provider / credentials

> Part of the AgentOS swappable-seam catalog — see the [seam index](../../interfaces.md).
> **Kind:** SOFT &nbsp;·&nbsp; **Implementations today:** 1 (Anthropic) &nbsp;·&nbsp; **Swap-readiness grade:** not separately graded

**Kind legend:** CLEAN = a real `Protocol`/typed port class · SOFT = swap via env/URL/prefix/wire, no code interface · NONE = not built yet.

## The black line

The model provider is swapped through config, not code: a base-URL override plus a
credential whose prefix routes it onto the right SDK auth env, targeting any
Anthropic-compatible endpoint. There is no provider interface to implement — a
config author fills in a base URL, a credential, and a model id, and the runner maps
them onto the variables the bundled Claude CLI authenticates from. What stays core is
the Anthropic wire format (kept even for OpenRouter, so prompt caching survives).

## Current contract

Resolution lives in `runner/src/agentos_runner/sdk_auth.py`. A provider is defined by
three things:

- **Credential**, delivered as `AGENTOS_CREDENTIALS` (`sdk_auth.py:31`).
  `resolve_model_credential` (`sdk_auth.py:82`) routes it by prefix
  (`sdk_auth.py:94`): `sk-ant-oat...` → `CLAUDE_CODE_OAUTH_TOKEN`; other `sk-ant-...`
  → `ANTHROPIC_API_KEY`; `sk-or-...` (OpenRouter) → base-URL override; a bare `sk-...`
  raises `UnsupportedCredentialError` (`sdk_auth.py:47`, `:116`). An SDK credential
  already in the env wins (`sdk_auth.py:89`).
- **Base URL**, `ANTHROPIC_BASE_URL` (`sdk_auth.py:34`). `resolve_base_url_override`
  (`sdk_auth.py:51`) builds the override env when set, and `resolve_sdk_env`
  (`sdk_auth.py:124`) is the entry point that decides override-vs-plain.
- **Model id**, `AGENTOS_MODEL`, read by `RunnerConfig.from_env`
  (`runner/src/agentos_runner/config.py:62`).

The override deliberately blanks `CLAUDE_CODE_OAUTH_TOKEN` and `ANTHROPIC_AUTH_TOKEN`
to `""` (`sdk_auth.py:76`) so an inherited token cannot take precedence or leak to a
third-party endpoint.

## Implementations today

One: Anthropic (direct API key or Claude Code OAuth token). OpenRouter is the
intended reference second provider and is already wired as a prefix branch
(`sdk_auth.py:100`): it reuses the shared base-URL-override seam, points at
`OPENROUTER_BASE_URL = "https://openrouter.ai/api"` (`sdk_auth.py:36`), and puts the
real key in `ANTHROPIC_API_KEY` (the `x-api-key` header OpenRouter reads).

## Known leakage

The gotcha the seam encodes rather than a bleed-through: base-URL override mode must
carry a **non-empty** placeholder key, `NO_OP_API_KEY = "not-needed"`
(`sdk_auth.py:44`). The bundled Claude CLI treats an empty `ANTHROPIC_API_KEY` as
"not logged in" and refuses to dial the overridden endpoint (empirically verified
2026-07-07, `sdk_auth.py:58`), so an empty string is not viable — a deliberately
non-`sk-` placeholder passes the auth gate without being mistakable for a credential.
The credential value is never logged.

## Cross-links

- **Epic(s):** #24 — bring your own model (OpenRouter + native Anthropic-format endpoints), the seam's forward work; #46 (closed) — the Ollama local-model demo mode, which also exercises this base-URL-override + credential path.
- **Vision doc:** [architecture-vision.md](../../architecture-vision.md) — core config seam, not one of the six swappable jobs.
- **ADR(s):** [ADR-0009](../../adr/0009-per-agent-connector-auth.md) — per-agent secrets and connector credentials (the model credential is the one credential the platform resolves today, via prefix mapping in `sdk_auth.py`).
