"""Map the ACI ``AGENTOS_CREDENTIALS`` reference onto the SDK's credential env.

The claude-agent-sdk authenticates from ``CLAUDE_CODE_OAUTH_TOKEN`` /
``ANTHROPIC_API_KEY`` in its own environment; it never reads the ACI
``AGENTOS_CREDENTIALS`` reference. When the platform supplies only
``AGENTOS_CREDENTIALS`` (the worker forwards it), the runner would otherwise drop
it and the real session would fail with a generic ``authentication_failed``. This
resolves it to the correct SDK variable before the session starts.

Rules:
- An explicit SDK credential already in the environment wins (the platform set it
  deliberately); this is then a no-op.
- ``sk-ant-...`` is an Anthropic API key -> ``ANTHROPIC_API_KEY``.
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
_SDK_CREDENTIAL_ENV = (OAUTH_TOKEN_ENV, API_KEY_ENV)


class UnsupportedCredentialError(RuntimeError):
    """``AGENTOS_CREDENTIALS`` is a recognizably non-Anthropic credential."""


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
    if credential.startswith("sk-ant-"):
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
