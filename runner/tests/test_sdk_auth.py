"""AGENTOS_CREDENTIALS -> SDK credential env resolution.

All values are fake placeholders; this never touches a real credential.
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
    resolve_model_credential,
    resolve_sdk_env,
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


@pytest.mark.parametrize("foreign", ["sk-PLACEHOLDER"])
def test_foreign_key_is_rejected_loudly(foreign: str) -> None:
    with pytest.raises(UnsupportedCredentialError) as exc:
        resolve_model_credential({CREDENTIALS_ENV: foreign})
    msg = str(exc.value)
    assert "Anthropic API key" in msg and "Claude Code OAuth token" in msg
    assert foreign not in msg  # the credential value never appears in the error


def test_openrouter_key_maps_to_api_key_not_bearer() -> None:
    env = {CREDENTIALS_ENV: "sk-or-PLACEHOLDER"}
    resolve_model_credential(env)

    assert env[BASE_URL_ENV] == OPENROUTER_BASE_URL
    # The real key goes in ANTHROPIC_API_KEY (the x-api-key header OpenRouter's
    # Anthropic endpoint reads), overriding the NO_OP_API_KEY placeholder.
    assert env[API_KEY_ENV] == "sk-or-PLACEHOLDER"
    # It must NOT land in ANTHROPIC_AUTH_TOKEN (Bearer); that stays blank.
    assert env[AUTH_TOKEN_ENV] == ""
    assert env[OAUTH_TOKEN_ENV] == ""


def test_explicit_sdk_credential_wins_over_openrouter_reference() -> None:
    env = {API_KEY_ENV: "sk-ant-EXPLICIT", CREDENTIALS_ENV: "sk-or-PLACEHOLDER"}
    resolve_model_credential(env)

    assert env[API_KEY_ENV] == "sk-ant-EXPLICIT"
    assert BASE_URL_ENV not in env


def test_no_credential_is_a_noop() -> None:
    env: dict[str, str] = {}
    resolve_model_credential(env)
    assert env == {}


def test_base_url_override_sets_sdk_base_url_and_placeholder_api_key() -> None:
    from agentos_runner.sdk_auth import (
        BASE_URL_ENV,
        NO_OP_API_KEY,
        resolve_base_url_override,
    )

    assert BASE_URL_ENV == "ANTHROPIC_BASE_URL"

    override = resolve_base_url_override({BASE_URL_ENV: "http://ollama:11434"})

    assert override is not None
    assert override[BASE_URL_ENV] == "http://ollama:11434"
    # The placeholder API key must be NON-EMPTY: an empty ANTHROPIC_API_KEY makes
    # the bundled Claude CLI report "not logged in" and skip the endpoint call.
    assert override[API_KEY_ENV] == NO_OP_API_KEY
    assert override[API_KEY_ENV] != ""
    # The OAuth token is blanked so an inherited token cannot win over the
    # placeholder + overridden base URL.
    assert override[OAUTH_TOKEN_ENV] == ""
    # The Bearer token is blanked too so an inherited ANTHROPIC_AUTH_TOKEN
    # cannot leak to the overridden (local/third-party) endpoint.
    assert override[AUTH_TOKEN_ENV] == ""


def test_base_url_override_is_absent_when_env_is_missing() -> None:
    from agentos_runner.sdk_auth import resolve_base_url_override

    assert resolve_base_url_override({}) is None


def test_base_url_override_is_absent_when_env_is_empty() -> None:
    from agentos_runner.sdk_auth import BASE_URL_ENV, resolve_base_url_override

    assert resolve_base_url_override({BASE_URL_ENV: ""}) is None


def test_resolve_sdk_env_ollama_returns_override_dict() -> None:
    # Base URL set, no sk-or- credential: generic override mode. Returns the
    # override dict for ClaudeAgentOptions.env; the Bearer token is blanked.
    env = {BASE_URL_ENV: "http://ollama:11434"}
    override = resolve_sdk_env(env)

    assert override is not None
    assert override[BASE_URL_ENV] == "http://ollama:11434"
    assert override[API_KEY_ENV] == NO_OP_API_KEY
    assert override[AUTH_TOKEN_ENV] == ""


def test_resolve_sdk_env_plain_anthropic_key_mutates_env() -> None:
    # sk-ant- credential, no base URL: returns None, mutates env with the API key.
    env = {CREDENTIALS_ENV: "sk-ant-PLACEHOLDER"}
    assert resolve_sdk_env(env) is None
    assert env[API_KEY_ENV] == "sk-ant-PLACEHOLDER"
    assert BASE_URL_ENV not in env


def test_resolve_sdk_env_openrouter_alone_mutates_env() -> None:
    # sk-or- credential, no preset base URL: returns None; env gets the
    # OpenRouter base URL and the real key in ANTHROPIC_API_KEY (x-api-key).
    env = {CREDENTIALS_ENV: "sk-or-PLACEHOLDER"}
    assert resolve_sdk_env(env) is None
    assert env[BASE_URL_ENV] == OPENROUTER_BASE_URL
    assert env[API_KEY_ENV] == "sk-or-PLACEHOLDER"
    assert env[AUTH_TOKEN_ENV] == ""


def test_resolve_sdk_env_openrouter_with_preset_base_url_sets_api_key() -> None:
    # P2 regression: an operator sets ANTHROPIC_BASE_URL AND passes an sk-or-
    # credential. The sk-or- key must still be routed (into ANTHROPIC_API_KEY, the
    # x-api-key header), not skipped by the generic base-URL override.
    env = {BASE_URL_ENV: "https://openrouter.ai/api", CREDENTIALS_ENV: "sk-or-PLACEHOLDER"}
    assert resolve_sdk_env(env) is None
    assert env[API_KEY_ENV] == "sk-or-PLACEHOLDER"
    assert env[AUTH_TOKEN_ENV] == ""
