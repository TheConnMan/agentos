"""Typed configuration for the dispatcher, parsed from the process environment.

The dispatcher needs two Slack tokens (an app-level token for Socket Mode and a
bot token for Web API calls), a Valkey connection, and the stream/dedupe/backoff
knobs. ``DispatcherConfig.from_env`` is the single sanctioned parser; it mirrors
the env-var handling style of ``aci_protocol.session.SessionConfig``.

Env mapping:
    SLACK_APP_TOKEN            -> slack_app_token   (xapp-..., Socket Mode)
    SLACK_BOT_TOKEN            -> slack_bot_token   (xoxb-..., Web API)
    SLACK_SIGNING_SECRET       -> slack_signing_secret (optional; unused in
                                  Socket Mode, kept for Bolt App construction)
    VALKEY_HOST                -> valkey_host
    VALKEY_PORT                -> valkey_port
    VALKEY_PASSWORD            -> valkey_password
    VALKEY_DB                  -> valkey_db
    AGENTOS_STREAM             -> stream
    AGENTOS_DEDUPE_PREFIX      -> dedupe_prefix
    AGENTOS_DEDUPE_TTL_SECONDS -> dedupe_ttl_seconds
    AGENTOS_PLACEHOLDER_TEXT   -> placeholder_text
    AGENTOS_BACKOFF_INITIAL_SECONDS -> backoff_initial_seconds
    AGENTOS_BACKOFF_MAX_SECONDS     -> backoff_max_seconds
    AGENTOS_BACKOFF_MULTIPLIER      -> backoff_multiplier
"""

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DispatcherConfig(BaseModel):
    """Everything the dispatcher needs to run, in one typed object."""

    model_config = ConfigDict(frozen=True)

    slack_app_token: str = ""
    slack_bot_token: str = ""
    slack_signing_secret: str = ""

    valkey_host: str = "localhost"
    valkey_port: int = 6379
    valkey_password: str = ""
    valkey_db: int = 0

    stream: str = "agentos:runs"
    dedupe_prefix: str = "agentos:dedupe:"
    dedupe_ttl_seconds: int = 3600

    placeholder_text: str = "On it. Working on your request."

    backoff_initial_seconds: float = Field(default=1.0, gt=0)
    backoff_max_seconds: float = Field(default=30.0, gt=0)
    backoff_multiplier: float = Field(default=2.0, gt=1)

    def dedupe_key(self, slack_event_id: str) -> str:
        """The Valkey key that guards a single Slack event id against retries."""
        return f"{self.dedupe_prefix}{slack_event_id}"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "DispatcherConfig":
        """Build a config from a process-environment mapping, using defaults for
        anything absent."""
        values: dict[str, Any] = {}
        _set_str(values, "slack_app_token", env, "SLACK_APP_TOKEN")
        _set_str(values, "slack_bot_token", env, "SLACK_BOT_TOKEN")
        _set_str(values, "slack_signing_secret", env, "SLACK_SIGNING_SECRET")
        _set_str(values, "valkey_host", env, "VALKEY_HOST")
        _set_int(values, "valkey_port", env, "VALKEY_PORT")
        _set_str(values, "valkey_password", env, "VALKEY_PASSWORD")
        _set_int(values, "valkey_db", env, "VALKEY_DB")
        _set_str(values, "stream", env, "AGENTOS_STREAM")
        _set_str(values, "dedupe_prefix", env, "AGENTOS_DEDUPE_PREFIX")
        _set_int(values, "dedupe_ttl_seconds", env, "AGENTOS_DEDUPE_TTL_SECONDS")
        _set_str(values, "placeholder_text", env, "AGENTOS_PLACEHOLDER_TEXT")
        _set_float(values, "backoff_initial_seconds", env, "AGENTOS_BACKOFF_INITIAL_SECONDS")
        _set_float(values, "backoff_max_seconds", env, "AGENTOS_BACKOFF_MAX_SECONDS")
        _set_float(values, "backoff_multiplier", env, "AGENTOS_BACKOFF_MULTIPLIER")
        return cls(**values)


def _set_str(
    values: dict[str, Any], key: str, env: Mapping[str, str], var: str
) -> None:
    if var in env:
        values[key] = env[var]


def _set_int(
    values: dict[str, Any], key: str, env: Mapping[str, str], var: str
) -> None:
    if var in env:
        values[key] = int(env[var])


def _set_float(
    values: dict[str, Any], key: str, env: Mapping[str, str], var: str
) -> None:
    if var in env:
        values[key] = float(env[var])
