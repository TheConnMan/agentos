"""Shared credential classification + frozen resolve_sdk_env parity (issue #311).

``classify_credential`` is the pure prefix classifier the plan extracts out of
``resolve_model_credential`` so the OpenCode binder can share it. These tests pin
the five classes and the OAuth-before-API-key order.

The ``test_parity_*`` cases call the public ``resolve_sdk_env`` /
``resolve_model_credential`` and assert their current outputs, proving the
refactor preserves the Claude binder byte-for-byte. They live here (not in the
frozen ``test_sdk_auth.py``) so the frozen gate stays untouched while the new
classifier is still exercised against the real routing.

All credential values are obviously-fake and split across a ``+`` so no real
token shape ever appears as a single literal.
"""

from __future__ import annotations

import pytest
from agentos_runner.sdk_auth import (
    API_KEY_ENV,
    AUTH_TOKEN_ENV,
    BASE_URL_ENV,
    CREDENTIALS_ENV,
    NO_OP_API_KEY,
    OAUTH_TOKEN_ENV,
    OPENROUTER_BASE_URL,
    UnsupportedCredentialError,
    classify_credential,
    resolve_model_credential,
    resolve_sdk_env,
)

# Fake credentials, one per class. Split literals keep the secrets hook quiet and
# make the fakeness obvious.
FAKE_OAUTH_TOKEN = "sk-ant-oat" + "01-FAKE"
FAKE_ANTHROPIC_KEY = "sk-ant-" + "api03-FAKE"
FAKE_OPENROUTER_KEY = "sk-or-" + "v1-FAKE"
FAKE_UNSUPPORTED_SK = "sk-" + "proj-FAKE"
FAKE_OPAQUE = "opaque-" + "FAKE-token"


# --- classify_credential: the five classes, order pinned ------------------------


def test_classify_oauth_token() -> None:
    assert classify_credential(FAKE_OAUTH_TOKEN) == "oauth-token"


def test_classify_anthropic_api_key() -> None:
    assert classify_credential(FAKE_ANTHROPIC_KEY) == "anthropic-api-key"


def test_classify_openrouter() -> None:
    assert classify_credential(FAKE_OPENROUTER_KEY) == "openrouter"


def test_classify_unsupported_sk() -> None:
    assert classify_credential(FAKE_UNSUPPORTED_SK) == "unsupported-sk"


def test_classify_opaque() -> None:
    assert classify_credential(FAKE_OPAQUE) == "opaque"


def test_classify_oauth_prefix_wins_over_api_key() -> None:
    # sk-ant-oat shares the sk-ant- prefix with a plain API key; the more
    # specific OAuth check must be first, so an OAuth token never classifies as
    # anthropic-api-key.
    result = classify_credential(FAKE_OAUTH_TOKEN)
    assert result == "oauth-token"
    assert result != "anthropic-api-key"


# --- frozen-behavior parity: resolve_sdk_env / resolve_model_credential ---------


def test_parity_openrouter_routes_key_to_api_key_env() -> None:
    env = {CREDENTIALS_ENV: FAKE_OPENROUTER_KEY}
    assert resolve_sdk_env(env) is None
    assert env[BASE_URL_ENV] == OPENROUTER_BASE_URL
    assert env[API_KEY_ENV] == FAKE_OPENROUTER_KEY
    assert env[AUTH_TOKEN_ENV] == ""
    assert env[OAUTH_TOKEN_ENV] == ""


def test_parity_anthropic_api_key_mutates_env() -> None:
    env = {CREDENTIALS_ENV: FAKE_ANTHROPIC_KEY}
    assert resolve_sdk_env(env) is None
    assert env[API_KEY_ENV] == FAKE_ANTHROPIC_KEY
    assert BASE_URL_ENV not in env


def test_parity_oauth_token_routes_to_oauth_env() -> None:
    env = {CREDENTIALS_ENV: FAKE_OAUTH_TOKEN}
    assert resolve_sdk_env(env) is None
    assert env[OAUTH_TOKEN_ENV] == FAKE_OAUTH_TOKEN
    assert API_KEY_ENV not in env


def test_parity_opaque_token_routes_to_oauth_env() -> None:
    env = {CREDENTIALS_ENV: FAKE_OPAQUE}
    assert resolve_sdk_env(env) is None
    assert env[OAUTH_TOKEN_ENV] == FAKE_OPAQUE


def test_parity_unsupported_sk_rejected_loudly() -> None:
    with pytest.raises(UnsupportedCredentialError):
        resolve_model_credential({CREDENTIALS_ENV: FAKE_UNSUPPORTED_SK})


def test_parity_openrouter_with_preset_base_url_still_routes_key() -> None:
    # The #229 regression: a preset ANTHROPIC_BASE_URL must not drop the sk-or-
    # key -- it is still routed into ANTHROPIC_API_KEY (the x-api-key header).
    env = {BASE_URL_ENV: OPENROUTER_BASE_URL, CREDENTIALS_ENV: FAKE_OPENROUTER_KEY}
    assert resolve_sdk_env(env) is None
    assert env[API_KEY_ENV] == FAKE_OPENROUTER_KEY
    assert env[AUTH_TOKEN_ENV] == ""


def test_parity_base_url_override_returns_dict() -> None:
    override = resolve_sdk_env({BASE_URL_ENV: "http://ollama:11434"})
    assert override is not None
    assert override[BASE_URL_ENV] == "http://ollama:11434"
    assert override[API_KEY_ENV] == NO_OP_API_KEY
    assert override[AUTH_TOKEN_ENV] == ""


def test_parity_no_credential_is_a_noop() -> None:
    env: dict[str, str] = {}
    assert resolve_sdk_env(env) is None
    assert env == {}
