"""OpenCode credential binder (issue #311 AC1).

``resolve_opencode_env`` maps an ``AGENTOS_CREDENTIALS`` reference onto the env
OpenCode reads. Only an OpenRouter (``sk-or-``) credential is supported; every
other class is rejected loudly (never silently dropped) with a message that names
``AGENTOS_CREDENTIALS`` and never echoes the credential value.

All credential values are obviously-fake and split across a ``+`` so no real
token shape appears as a single literal.
"""

from __future__ import annotations

import pytest
from agentos_runner.opencode.auth import resolve_opencode_env
from agentos_runner.sdk_auth import CREDENTIALS_ENV, UnsupportedCredentialError

OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"

FAKE_OPENROUTER_KEY = "sk-or-" + "v1-FAKE-abc"
FAKE_OAUTH_TOKEN = "sk-ant-oat" + "01-FAKE"
FAKE_ANTHROPIC_KEY = "sk-ant-" + "api03-FAKE"
FAKE_UNSUPPORTED_SK = "sk-" + "proj-FAKE"
FAKE_OPAQUE = "opaque-" + "FAKE-token"


def test_openrouter_credential_maps_to_openrouter_api_key_only() -> None:
    out = resolve_opencode_env({CREDENTIALS_ENV: FAKE_OPENROUTER_KEY})
    # Exactly the one bound var -- nothing else.
    assert out == {OPENROUTER_API_KEY_ENV: FAKE_OPENROUTER_KEY}


@pytest.mark.parametrize(
    "credential",
    [FAKE_OAUTH_TOKEN, FAKE_ANTHROPIC_KEY, FAKE_UNSUPPORTED_SK, FAKE_OPAQUE],
)
def test_non_openrouter_classes_rejected_loudly(credential: str) -> None:
    with pytest.raises(UnsupportedCredentialError) as exc:
        resolve_opencode_env({CREDENTIALS_ENV: credential})
    message = str(exc.value)
    # Names the ACI reference the operator must fix ...
    assert "AGENTOS_CREDENTIALS" in message
    # ... and never echoes the credential value (no-echo discipline).
    assert credential not in message


def test_rejection_never_silently_drops_the_credential() -> None:
    # An unsupported credential must raise, not return an empty/partial env that
    # would let the session fall through to no-credential (silent drop).
    with pytest.raises(UnsupportedCredentialError):
        resolve_opencode_env({CREDENTIALS_ENV: FAKE_ANTHROPIC_KEY})


def test_unset_credentials_is_empty_noop() -> None:
    assert resolve_opencode_env({}) == {}


def test_empty_credentials_is_empty_noop() -> None:
    # Matches the Claude binder's empty-string no-op contract; never injects an
    # empty OPENROUTER_API_KEY.
    assert resolve_opencode_env({CREDENTIALS_ENV: ""}) == {}
