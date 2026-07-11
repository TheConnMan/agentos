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
    DEEPSEEK_BASE_URL,
    MODEL_BASE_URL_ENV,
    MOONSHOT_BASE_URL,
    NO_OP_API_KEY,
    OAUTH_TOKEN_ENV,
    OPENROUTER_BASE_URL,
    PROVIDER_BASE_URLS,
    ZHIPU_BASE_URL,
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


# --- Provider-native Anthropic-compatible endpoints (#252): Zhipu / Moonshot /
# DeepSeek. Selected by base URL; the provider key is forwarded as x-api-key.


def test_provider_base_urls_stay_on_anthropic_format() -> None:
    # The canonical endpoints keep the Anthropic wire format (SDK appends
    # /v1/messages) so provider automatic prefix caching survives.
    assert PROVIDER_BASE_URLS["zhipu"] == ZHIPU_BASE_URL == "https://api.z.ai/api/anthropic"
    assert PROVIDER_BASE_URLS["moonshot"] == MOONSHOT_BASE_URL
    assert PROVIDER_BASE_URLS["deepseek"] == DEEPSEEK_BASE_URL
    assert PROVIDER_BASE_URLS["openrouter"] == OPENROUTER_BASE_URL


@pytest.mark.parametrize(
    "base_url",
    [ZHIPU_BASE_URL, MOONSHOT_BASE_URL, DEEPSEEK_BASE_URL],
)
def test_provider_native_key_forwarded_as_x_api_key(base_url: str) -> None:
    # Moonshot / DeepSeek use OpenAI-style sk- keys; Zhipu uses a non-sk key.
    # With the provider base URL set, the key must be forwarded to
    # ANTHROPIC_API_KEY (x-api-key), overriding the NO_OP placeholder -- not
    # rejected and not dropped.
    env = {BASE_URL_ENV: base_url, CREDENTIALS_ENV: "sk-PROVIDER-PLACEHOLDER"}
    override = resolve_sdk_env(env)

    assert override is not None
    assert override[BASE_URL_ENV] == base_url
    assert override[API_KEY_ENV] == "sk-PROVIDER-PLACEHOLDER"
    # Inherited OAuth / Bearer tokens stay blanked so they cannot leak to the
    # third-party endpoint.
    assert override[OAUTH_TOKEN_ENV] == ""
    assert override[AUTH_TOKEN_ENV] == ""


def test_zhipu_non_sk_key_forwarded() -> None:
    # Zhipu keys are id.secret shaped (no sk- prefix); still forwarded.
    env = {BASE_URL_ENV: ZHIPU_BASE_URL, CREDENTIALS_ENV: "zhipu-id.PLACEHOLDER"}
    override = resolve_sdk_env(env)
    assert override is not None
    assert override[API_KEY_ENV] == "zhipu-id.PLACEHOLDER"


def test_model_base_url_alias_selects_override() -> None:
    # AGENTOS_MODEL_BASE_URL (AGENTOS_-namespaced alias) drives the same seam.
    env = {MODEL_BASE_URL_ENV: DEEPSEEK_BASE_URL, CREDENTIALS_ENV: "sk-DEEPSEEK-PLACEHOLDER"}
    override = resolve_sdk_env(env)
    assert override is not None
    assert override[BASE_URL_ENV] == DEEPSEEK_BASE_URL
    assert override[API_KEY_ENV] == "sk-DEEPSEEK-PLACEHOLDER"


def test_raw_base_url_wins_over_alias() -> None:
    env = {
        BASE_URL_ENV: MOONSHOT_BASE_URL,
        MODEL_BASE_URL_ENV: DEEPSEEK_BASE_URL,
        CREDENTIALS_ENV: "sk-PLACEHOLDER",
    }
    override = resolve_sdk_env(env)
    assert override is not None
    assert override[BASE_URL_ENV] == MOONSHOT_BASE_URL


def test_oauth_token_not_forwarded_to_provider_endpoint() -> None:
    # A Claude Code OAuth token must never be forwarded to a third-party
    # endpoint; the placeholder stays and the token is blanked (hermetic).
    env = {BASE_URL_ENV: ZHIPU_BASE_URL, CREDENTIALS_ENV: "sk-ant-oatPLACEHOLDER"}
    override = resolve_sdk_env(env)
    assert override is not None
    assert override[API_KEY_ENV] == NO_OP_API_KEY
    assert override[OAUTH_TOKEN_ENV] == ""


def test_ollama_without_credential_keeps_placeholder() -> None:
    # No credential (local Ollama): the NO_OP placeholder is retained.
    env = {BASE_URL_ENV: "http://ollama:11434"}
    override = resolve_sdk_env(env)
    assert override is not None
    assert override[API_KEY_ENV] == NO_OP_API_KEY


def test_bare_sk_still_rejected_without_base_url() -> None:
    # The direct-Anthropic path (no base URL) still rejects a bare sk- key: there
    # is no provider endpoint to forward it to.
    with pytest.raises(UnsupportedCredentialError):
        resolve_sdk_env({CREDENTIALS_ENV: "sk-PLACEHOLDER"})
