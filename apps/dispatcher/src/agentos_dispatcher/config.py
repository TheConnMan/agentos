"""Typed configuration for the dispatcher, read from the process environment.

The dispatcher needs two Slack tokens (an app-level token for Socket Mode and a
bot token for Web API calls), a Valkey connection, and the stream/dedupe/backoff
knobs. ``DispatcherConfig`` is a ``pydantic_settings.BaseSettings`` (the house
pattern, see ``apps/api``): construct it with no arguments and it reads the
environment on init, falling back to the defaults below for anything absent.

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
    AGENTOS_SHIMMER            -> shimmer (assistant-thread status while working)
    AGENTOS_BACKOFF_INITIAL_SECONDS -> backoff_initial_seconds
    AGENTOS_BACKOFF_MAX_SECONDS     -> backoff_max_seconds
    AGENTOS_BACKOFF_MULTIPLIER      -> backoff_multiplier
    AGENTOS_API_BASE_URL       -> api_base_url
    AGENTOS_API_KEY            -> api_key
    AGENTOS_API_PREFLIGHT_TIMEOUT_SECONDS -> api_preflight_timeout_s
    AGENTOS_HEARTBEAT_FILE             -> heartbeat_file
    AGENTOS_HEARTBEAT_INTERVAL_SECONDS -> heartbeat_interval_s
"""

from typing import Annotated

from pydantic import BeforeValidator, Field
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import (
    EnvSettingsSource,
    PydanticBaseSettingsSource,
)


class _AliasOnlyEnvSource(EnvSettingsSource):
    """Env source that reads an aliased field ONLY from its ``validation_alias``.

    ``populate_by_name=True`` is set so tests can construct the config with
    field-name kwargs. But in pydantic-settings that same flag makes the default
    env source append the bare uppercased field name as a fallback env key for
    every aliased field -- so ``stream`` (alias ``AGENTOS_STREAM``) would also
    silently read a stray ``STREAM``. That breaks the behavior-preserving
    contract of the refactor. We drop the field-name fallback for aliased
    fields; non-aliased fields keep reading their plain uppercased name, and
    kwarg population is untouched (it runs through the init source, not here).
    """

    def _extract_field_info(
        self, field: FieldInfo, field_name: str
    ) -> list[tuple[str, str, bool]]:
        infos = super()._extract_field_info(field, field_name)
        if field.validation_alias is not None:
            infos = [info for info in infos if info[0] != field_name]
        return infos


def _parse_bool(value: object) -> bool:
    """Parse the truthy env-string set the dispatcher has always accepted.

    A real bool passes through (so kwarg construction in tests is unchanged); any
    other string is truthy only when it is one of the accepted tokens, matching
    the previous hand-rolled ``_set_bool``.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


Bool = Annotated[bool, BeforeValidator(_parse_bool)]


class DispatcherConfig(BaseSettings):
    """Everything the dispatcher needs to run, in one typed object."""

    model_config = SettingsConfigDict(
        frozen=True, populate_by_name=True, extra="ignore"
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Swap the env source so aliased fields read only their alias."""
        return (
            init_settings,
            _AliasOnlyEnvSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )

    slack_app_token: str = ""
    slack_bot_token: str = ""
    slack_signing_secret: str = ""

    valkey_host: str = "localhost"
    valkey_port: int = 6379
    valkey_password: str = ""
    valkey_db: int = 0

    stream: str = Field(default="agentos:runs", validation_alias="AGENTOS_STREAM")
    dedupe_prefix: str = Field(
        default="agentos:dedupe:", validation_alias="AGENTOS_DEDUPE_PREFIX"
    )
    dedupe_ttl_seconds: int = Field(
        default=3600, validation_alias="AGENTOS_DEDUPE_TTL_SECONDS"
    )

    # Platform API for the approval click-to-resolve flow (#246). Defaults match
    # the API's dev stack, mirroring the worker's settings for the same seam.
    api_base_url: str = Field(
        default="http://localhost:8000", validation_alias="AGENTOS_API_BASE_URL"
    )
    api_key: str = Field(default="agentos-dev-key", validation_alias="AGENTOS_API_KEY")
    # Deadline for the boot-time gate on that wiring (see preflight.py). Long
    # enough to absorb the API's own startup. Must be positive: the gate is the
    # AC2 requirement, so a non-positive value is a config error at boot rather
    # than a silent way to turn it off. Non-finite is rejected for the same
    # reason: `inf` passes `gt=0` but hangs the boot gate forever, so the pod
    # never exits, never crash-loops, and the operator never gets the signal the
    # gate exists to produce.
    api_preflight_timeout_s: float = Field(
        default=30.0,
        gt=0,
        allow_inf_nan=False,
        validation_alias="AGENTOS_API_PREFLIGHT_TIMEOUT_SECONDS",
    )

    placeholder_text: str = Field(
        default="On it. Working on your request.",
        validation_alias="AGENTOS_PLACEHOLDER_TEXT",
    )
    # When true, also set a Slack assistant-thread status (the native "shimmer"
    # on the app name) to placeholder_text while a turn runs. The worker clears it
    # when the turn ends. Off by default; requires the app's assistant feature +
    # assistant:write scope (see slack-app-manifest.yaml).
    shimmer: Bool = Field(default=False, validation_alias="AGENTOS_SHIMMER")

    backoff_initial_seconds: float = Field(
        default=1.0, gt=0, validation_alias="AGENTOS_BACKOFF_INITIAL_SECONDS"
    )
    backoff_max_seconds: float = Field(
        default=30.0, gt=0, validation_alias="AGENTOS_BACKOFF_MAX_SECONDS"
    )
    backoff_multiplier: float = Field(
        default=2.0, gt=1, validation_alias="AGENTOS_BACKOFF_MULTIPLIER"
    )

    # A daemon thread touches heartbeat_file every heartbeat_interval_s so an exec
    # liveness probe can restart the pod if the process fully wedges.
    heartbeat_file: str = Field(
        default="/tmp/agentos-dispatcher.heartbeat",
        validation_alias="AGENTOS_HEARTBEAT_FILE",
    )
    heartbeat_interval_s: float = Field(
        default=10.0, validation_alias="AGENTOS_HEARTBEAT_INTERVAL_SECONDS"
    )

    def dedupe_key(self, slack_event_id: str) -> str:
        """The Valkey key that guards a single Slack event id against retries."""
        return f"{self.dedupe_prefix}{slack_event_id}"
