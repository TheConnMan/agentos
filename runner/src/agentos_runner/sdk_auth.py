"""Map the ACI ``AGENTOS_CREDENTIALS`` reference onto the SDK's credential env.

The claude-agent-sdk authenticates from ``CLAUDE_CODE_OAUTH_TOKEN`` /
``ANTHROPIC_API_KEY`` in its own environment; it never reads the ACI
``AGENTOS_CREDENTIALS`` reference. When the platform supplies only
``AGENTOS_CREDENTIALS`` (the worker forwards it), the runner would otherwise drop
it and the real session would fail with a generic ``authentication_failed``. This
resolves it to the correct SDK variable before the session starts.

Rules (order matters -- an OAuth token and an API key share the ``sk-ant-``
prefix, so the more specific OAuth prefix is checked first):
- An explicit SDK credential already in the environment wins (the platform set it
  deliberately); this is then a no-op.
- ``sk-ant-oat...`` is a Claude Code OAuth token -> ``CLAUDE_CODE_OAUTH_TOKEN``.
- ``sk-ant-...`` (any other) is an Anthropic API key -> ``ANTHROPIC_API_KEY``.
- A recognizably non-Anthropic key (``sk-or-...`` OpenRouter, bare ``sk-...``
  OpenAI-style) is rejected loudly, naming what IS supported, rather than handed
  to the SDK to die as a generic auth failure. Other providers are post-MVP.
- Anything else is treated as a Claude Code OAuth token ->
  ``CLAUDE_CODE_OAUTH_TOKEN``.

The credential value (and its length) is never logged or echoed.
"""

from __future__ import annotations

from collections.abc import MutableMapping

CREDENTIALS_ENV = "AGENTOS_CREDENTIALS"
OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
API_KEY_ENV = "ANTHROPIC_API_KEY"
BASE_URL_ENV = "ANTHROPIC_BASE_URL"
_SDK_CREDENTIAL_ENV = (OAUTH_TOKEN_ENV, API_KEY_ENV)

# Non-empty no-op placeholder API key used in base-URL override mode. It is
# deliberately not sk-... shaped so it can never be mistaken for a real
# credential. The bundled Claude CLI treats an EMPTY ANTHROPIC_API_KEY as "not
# logged in" and refuses to call the endpoint, so the placeholder must be
# non-empty to pass the CLI's auth gate before it dials the overridden base URL.
NO_OP_API_KEY = "not-needed"


class UnsupportedCredentialError(RuntimeError):
    """``AGENTOS_CREDENTIALS`` is a recognizably non-Anthropic credential."""


def resolve_base_url_override(env: MutableMapping[str, str]) -> dict[str, str] | None:
    """Build the generic SDK base URL override env when configured.

    This is the provider agnostic base URL override path: local Ollama today and
    OpenRouter/#24 next. It targets any Anthropic-compatible endpoint without a
    real Anthropic credential.

    The placeholder API key is NON-EMPTY on purpose (empirically verified
    2026-07-07): the bundled Claude CLI treats an empty ANTHROPIC_API_KEY as "not
    logged in" and refuses to call the endpoint before it ever reaches the base
    URL, so it must carry a non-credential placeholder to pass the auth gate.
    Paired with the overridden base URL, that placeholder cannot authenticate
    against real Anthropic.

    CLAUDE_CODE_OAUTH_TOKEN is blanked to "" so an inherited OAuth token cannot
    take precedence over the placeholder + base URL, keeping override mode
    hermetic. The base URL is not a secret, but this module keeps the same no
    echo discipline it uses for credentials.
    """
    base_url = env.get(BASE_URL_ENV)
    if not base_url:
        return None
    return {
        BASE_URL_ENV: base_url,
        API_KEY_ENV: NO_OP_API_KEY,
        OAUTH_TOKEN_ENV: "",
    }


def resolve_model_credential(env: MutableMapping[str, str]) -> None:
    """Populate the SDK credential env from ``AGENTOS_CREDENTIALS`` when needed.

    Mutates ``env`` in place. No-op when an SDK credential is already present or
    ``AGENTOS_CREDENTIALS`` is unset. Raises ``UnsupportedCredentialError`` for a
    recognizably non-Anthropic key.
    """
    if any(env.get(var) for var in _SDK_CREDENTIAL_ENV):
        return  # an explicit SDK credential wins
    credential = env.get(CREDENTIALS_ENV)
    if not credential:
        return
    if credential.startswith("sk-ant-oat"):
        # Claude Code OAuth tokens share the sk-ant- prefix with API keys, so
        # this more specific check must come first.
        env[OAUTH_TOKEN_ENV] = credential
    elif credential.startswith("sk-ant-"):
        env[API_KEY_ENV] = credential
    elif credential.startswith("sk-"):
        # OpenAI-style ("sk-..."), OpenRouter ("sk-or-..."), and similar. Fail
        # loudly rather than forwarding a key the Anthropic SDK cannot use.
        raise UnsupportedCredentialError(
            "AGENTOS_CREDENTIALS is not a supported model credential. Provide an "
            "Anthropic API key (sk-ant-...) or a Claude Code OAuth token."
        )
    else:
        env[OAUTH_TOKEN_ENV] = credential
