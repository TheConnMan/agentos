# ADR-0048: Declare the model endpoint's wire protocol and credential keys

Status: Accepted
Date: 2026-07-17

## Context

The runner resolves a BYO model endpoint in `runner/src/agentos_runner/sdk_auth.py`.
Two things about that endpoint were assumed rather than declared (#514):

- **Wire protocol.** Any base-URL override was simply assumed to speak the
  Anthropic Messages format, because that is the only format claude-agent-sdk can
  dial. An operator who pointed `AGENTOS_MODEL_BASE_URL` at an OpenAI-shaped
  `/chat/completions` endpoint got a confusing runtime failure from deep inside
  the SDK rather than a clear config error.
- **Credential source.** The credential was read from exactly one hardcoded env
  var, `AGENTOS_CREDENTIALS`, and the "is it set" test conflated unset with
  empty-string — the #229 gotcha, where an empty `ANTHROPIC_API_KEY` is a live
  footgun rather than a harmless absence.

Prior art: xai-org's grok-build declares the wire protocol as an explicit
`api_backend` enum and accepts `env_key` as either a string or an array of names.

## Decision

**`AGENTOS_MODEL_API_BACKEND` declares the wire protocol** as an `ApiBackend`
enum (`runner/src/agentos_runner/sdk_auth.py::ApiBackend`): `messages` |
`chat_completions` | `responses`. Unset or empty defaults to `messages`
(`runner/src/agentos_runner/sdk_auth.py::DEFAULT_API_BACKEND`), which is exactly
the pre-change assumption, so no existing config changes behavior.

**`ApiBackend.speaks_anthropic_wire` is the single deterministic branch point**
(`runner/src/agentos_runner/sdk_auth.py::ApiBackend.speaks_anthropic_wire`).
`resolve_sdk_env` (`runner/src/agentos_runner/sdk_auth.py::resolve_sdk_env`)
checks it FIRST, before any credential or base-URL branching, so an undialable
backend is rejected on the declaration itself rather than as a side effect of
whichever path config happens to select. `chat_completions` and `responses` are
declarable but rejected with an actionable message: the runner speaks Anthropic
Messages via claude-agent-sdk, and reaching an OpenAI-shaped endpoint needs a
translating proxy in front of the runner. They are in the enum because naming the
thing you cannot do is what makes the rejection legible; a value the enum does not
know raises the same error
(`runner/src/agentos_runner/sdk_auth.py::UnsupportedApiBackendError`).

**`AGENTOS_MODEL_ENV_KEY` declares which env var carries the credential**: a bare
name or a JSON array of names (`runner/src/agentos_runner/sdk_auth.py::parse_env_keys`).
Keys are walked in declared order and the first that is both set and non-empty
wins (`runner/src/agentos_runner/sdk_auth.py::resolve_credential`); a
present-but-empty key is skipped, never wins. Unset or empty defaults to
`("AGENTOS_CREDENTIALS",)`
(`runner/src/agentos_runner/sdk_auth.py::DEFAULT_CREDENTIAL_ENV_KEYS`),
byte-identical to today.

**Both new names are `AGENTOS_`-namespaced** per
[ADR-0047](0047-canonical-env-var-names.md), so the existing prefix rule in
`packages/plugin-format/src/plugin_format/reserved_env.py` already fences a
connector secret from shadowing them. They are enumerated in `_AGENTOS_BOOT_KEYS`
for greppability and to satisfy the completeness pin.

**Scope: runner-local env config, deliberately NOT an `aci-protocol`
`SessionConfig` field.** Model base URL and credential already flow as boot env
outside that frozen contract; putting these two beside them keeps the change out
of the frozen wire protocol. The alternative — adding them to `SessionConfig` —
was weighed and rejected: it would be a frozen-contract change requiring a
`PROTOCOL_VERSION` bump for no gain today.

**Trust boundary, considered.** `AGENTOS_MODEL_ENV_KEY` lets operator-supplied
config name an arbitrary env var to read a credential from. That is the same trust
level as `AGENTOS_MODEL_BASE_URL`, which can already redirect the session; boot env
is operator-controlled, and a connector secret cannot declare either name (reserved
prefix). No new privilege is granted.

## Consequences

- The protocol is self-documenting per endpoint, and a mis-declared endpoint fails
  fast with a fix instead of a deep SDK error.
- The empty-string credential footgun is closed by construction: an empty value is
  "not supplied", never a credential.
- A config author can source the credential from an env var that already exists
  (e.g. `["ANTHROPIC_AUTH_TOKEN","LC_ANTHROPIC_AUTH_TOKEN"]`) without renaming it.
- Adding real `chat_completions` / `responses` support later means adding a
  translating adapter behind the same enum, with no config-surface change.
- The enum has one dialable member today, so it buys legibility and a fast failure
  rather than new reach.
