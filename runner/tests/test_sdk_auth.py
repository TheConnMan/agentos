"""AGENTOS_CREDENTIALS -> SDK credential env resolution.

All values are fake placeholders; this never touches a real credential.
"""

from __future__ import annotations

import pytest
from agentos_runner.sdk_auth import (
    API_KEY_ENV,
    CREDENTIALS_ENV,
    OAUTH_TOKEN_ENV,
    UnsupportedCredentialError,
    resolve_model_credential,
)


def test_anthropic_api_key_maps_to_api_key_env() -> None:
    env = {CREDENTIALS_ENV: "sk-ant-PLACEHOLDER"}
    resolve_model_credential(env)
    assert env[API_KEY_ENV] == "sk-ant-PLACEHOLDER"
    assert OAUTH_TOKEN_ENV not in env


def test_oauth_token_with_sk_ant_oat_prefix_maps_to_oauth_env() -> None:
    # Claude Code OAuth tokens begin with sk-ant-oat and share the sk-ant-
    # prefix with API keys; they must route to the OAuth var, not the API key.
    env = {CREDENTIALS_ENV: "sk-ant-oatPLACEHOLDER"}
    resolve_model_credential(env)
    assert env[OAUTH_TOKEN_ENV] == "sk-ant-oatPLACEHOLDER"
    assert API_KEY_ENV not in env


def test_oauth_token_maps_to_oauth_env() -> None:
    # A Claude Code OAuth token is not an sk- key.
    env = {CREDENTIALS_ENV: "oauth-PLACEHOLDER-token"}
    resolve_model_credential(env)
    assert env[OAUTH_TOKEN_ENV] == "oauth-PLACEHOLDER-token"
    assert API_KEY_ENV not in env


def test_explicit_sdk_credential_wins_over_agentos_credentials() -> None:
    env = {OAUTH_TOKEN_ENV: "explicit-PLACEHOLDER", CREDENTIALS_ENV: "sk-ant-PLACEHOLDER"}
    resolve_model_credential(env)
    # The explicit SDK var is untouched; the reference is ignored (no API key set).
    assert env[OAUTH_TOKEN_ENV] == "explicit-PLACEHOLDER"
    assert API_KEY_ENV not in env


@pytest.mark.parametrize("foreign", ["sk-or-PLACEHOLDER", "sk-PLACEHOLDER"])
def test_foreign_key_is_rejected_loudly(foreign: str) -> None:
    with pytest.raises(UnsupportedCredentialError) as exc:
        resolve_model_credential({CREDENTIALS_ENV: foreign})
    msg = str(exc.value)
    assert "Anthropic API key" in msg and "Claude Code OAuth token" in msg
    assert foreign not in msg  # the credential value never appears in the error


def test_no_credential_is_a_noop() -> None:
    env: dict[str, str] = {}
    resolve_model_credential(env)
    assert env == {}
