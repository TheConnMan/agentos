"""Typed configuration for the worker kernel, read from the environment.

The kernel needs a Valkey connection (the stream it consumes plus its locks and
markers), a Slack bot token (to edit the placeholder in place), the stream and
consumer-group identity, and the tunables for retry, per-thread locking, and
crash-recovery reclaim. Substrate wiring (namespace, warm pool, runner port)
lives in ``SubstrateConfig`` and is assembled separately by the entrypoint.

``WorkerConfig`` is a ``pydantic_settings.BaseSettings`` (the house pattern, see
``apps/api``): construct it with no arguments and it reads the environment on
init, falling back to the defaults below for anything absent. The AGENTOS_-prefixed
knobs map through per-field ``validation_alias``; the rest read the uppercased
field name (VALKEY_HOST, DATABASE_URL, S3_ENDPOINT_URL, LANGFUSE_HOST, ...).
"""

from __future__ import annotations

import os
import socket
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
    field-name kwargs (``WorkerConfig(fake_model=True)``). But in
    pydantic-settings that same flag makes the default env source append the
    bare uppercased field name as a fallback env key for every aliased field --
    so ``api_key`` (alias ``AGENTOS_API_KEY``) would also silently read a stray
    ``API_KEY``. That breaks the behavior-preserving contract of the refactor.
    We drop the field-name fallback for aliased fields; non-aliased fields keep
    reading their plain uppercased name, and kwarg population is untouched
    (it runs through the init source, not here).
    """

    def _extract_field_info(
        self, field: FieldInfo, field_name: str
    ) -> list[tuple[str, str, bool]]:
        infos = super()._extract_field_info(field, field_name)
        if field.validation_alias is not None:
            infos = [info for info in infos if info[0] != field_name]
        return infos


def _default_consumer_name() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


def _parse_bool(value: object) -> bool:
    """Parse the truthy env-string set the worker has always accepted.

    A real bool passes through (so kwarg construction in tests is unchanged); any
    other string is truthy only when it is one of the accepted tokens, matching
    the previous hand-rolled ``_b`` (note: the worker does not treat "on" as
    truthy, unlike the dispatcher).
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return bool(value)


Bool = Annotated[bool, BeforeValidator(_parse_bool)]


class WorkerConfig(BaseSettings):
    """Everything the kernel needs, in one typed object."""

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

    # Valkey
    valkey_host: str = "localhost"
    valkey_port: int = 6379
    valkey_password: str = ""
    valkey_db: int = 0

    # Slack
    slack_bot_token: str = ""
    # Optional Slack Web API base URL override. Unset = the real Slack API; set it
    # to point chat.update at a local Slack stub (the CLI's `agentos local message`
    # no-Slack middle-mode e2e).
    slack_api_base_url: str = ""

    # Postgres (read-only): resolve channel -> agent -> deployment -> version.
    # Matches the API's DATABASE_URL / DB_SCHEMA so the worker reads the same DB.
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:25432/postgres"
    db_schema: str = "agentos"

    # Deployment-to-runtime binding. The plugin dir is the local path the runner
    # reads; sandbox provisioning fetches AGENTOS_BUNDLE_REF into it. Platform
    # default budget applies when an agent's budget columns are NULL.
    bundle_plugin_dir: str = Field(
        default="/bundles/current", validation_alias="AGENTOS_PLUGIN_DIR"
    )
    default_max_usd_per_day: float = 10.0
    default_max_output_tokens_per_run: int = 100000

    # Runner model + credentials passthrough (injected per-claim into every boot
    # env). fake_model runs the runner's canned FakeModelSession (no Anthropic
    # call, no credential needed) -- the middle-mode default on a laptop.
    # credentials is the opaque AGENTOS_CREDENTIALS the runner forwards to the
    # model call; it is never logged and never required when fake_model is on.
    fake_model: Bool = Field(default=False, validation_alias="AGENTOS_FAKE_MODEL")
    credentials: str = Field(default="", validation_alias="AGENTOS_CREDENTIALS")
    # Local model demo path: the worker can point the runner at an
    # Anthropic-compatible local endpoint without changing the fake-model default.
    model_base_url: str = Field(default="", validation_alias="AGENTOS_MODEL_BASE_URL")
    model: str = Field(default="", validation_alias="AGENTOS_MODEL")

    # When true, clear the Slack assistant-thread status (the "shimmer" the
    # dispatcher set) once a turn ends -- editing the placeholder does not
    # auto-clear it. Off by default; pairs with the dispatcher's AGENTOS_SHIMMER.
    shimmer: Bool = Field(default=False, validation_alias="AGENTOS_SHIMMER")

    # Stream / consumer group (must match the dispatcher's AGENTOS_STREAM)
    stream: str = Field(default="agentos:runs", validation_alias="AGENTOS_STREAM")
    consumer_group: str = Field(
        default="agentos-workers", validation_alias="AGENTOS_CONSUMER_GROUP"
    )
    consumer_name: str = Field(
        default_factory=_default_consumer_name,
        validation_alias="AGENTOS_CONSUMER_NAME",
    )

    # Read loop
    read_count: int = 16
    read_block_ms: int = 5000

    # Per-thread lock (serializes the routing decision + turn opening across
    # workers so a thread never has two live sessions). The TTL must exceed the
    # worst-case critical section (a cold claim can take up to the substrate's
    # claim_timeout, default 90s) so the lock never lapses mid-section and lets a
    # second worker open a concurrent turn. 90s claim + slack/route overhead stays
    # safely under this 120s TTL; if you raise claim_timeout keep it below this.
    lock_ttl_ms: int = 120000
    lock_acquire_timeout_s: float = 45.0
    lock_poll_interval_s: float = 0.02

    # Retry (flag-clean failures only; see the no-retry-after-side-effects rule)
    max_attempts: int = Field(default=3, validation_alias="AGENTOS_MAX_ATTEMPTS")
    retry_backoff_base_s: float = Field(default=1.0, gt=0)
    retry_backoff_max_s: float = Field(default=20.0, gt=0)

    # Markers
    idempotency_ttl_s: int = 86400

    # Crash recovery: reclaim stream entries pending longer than this, and run
    # the orphan-claim reaper, on this cadence. The idle threshold must exceed the
    # longest legitimate in-flight time (a turn can stream up to
    # runner_total_timeout_s, 600s) so the reaper never reclaims an entry a live
    # turn is still processing; the consumer additionally skips its own in-flight
    # entry ids as a second guard.
    reclaim_min_idle_ms: int = 900000
    reclaim_interval_s: float = 30.0

    # Slack placeholder edits are throttled to avoid rate limits while streaming.
    slack_edit_min_interval_s: float = 0.7

    # Runner HTTP timeouts
    runner_connect_timeout_s: float = 10.0
    runner_total_timeout_s: float = 600.0

    # Eval stream (F3): a separate consumer group on agentos:evals runs eval
    # suites and reports results to the platform API and Langfuse.
    eval_stream: str = Field(
        default="agentos:evals", validation_alias="AGENTOS_EVAL_STREAM"
    )
    eval_consumer_group: str = Field(
        default="agentos-eval-workers", validation_alias="AGENTOS_EVAL_CONSUMER_GROUP"
    )
    eval_consumer_name: str = Field(default_factory=_default_consumer_name)
    # On first creation the eval group starts at (now - this window) rather than
    # the stream head, so a backlog of ancient entries is not replayed en masse
    # on boot. Recent entries (younger than the window) are still delivered, so a
    # short outage never drops a live eval; only long-dead entries are skipped.
    eval_stream_max_age_hours: int = Field(
        default=24, validation_alias="AGENTOS_EVAL_STREAM_MAX_AGE_HOURS"
    )
    # MinIO / S3 for plugin bundles (mirrors the API's env names). The consumer
    # fetches a version's bundle by bundle_ref and loads its evals/ suite.
    s3_endpoint_url: str = "http://localhost:29000"
    s3_access_key: str = "minio"
    s3_secret_key: str = "miniosecret"
    s3_region: str = "us-east-1"
    bundle_bucket: str = "agentos-bundles"
    # Platform API for POST /evals/report. Defaults match the API's dev stack
    # (README serves it on :8000; its shared dev key is agentos-dev-key).
    api_base_url: str = Field(
        default="http://localhost:8000", validation_alias="AGENTOS_API_BASE_URL"
    )
    api_key: str = Field(default="agentos-dev-key", validation_alias="AGENTOS_API_KEY")
    report_max_attempts: int = 3
    report_backoff_base_s: float = Field(default=0.5, gt=0)
    # Langfuse for recording eval scores (the matrix reads them back by version).
    langfuse_host: str = "http://localhost:23000"
    langfuse_public_key: str = "pk-lf-agentos-dev"
    langfuse_secret_key: str = "sk-lf-agentos-dev"

    # The worker's asyncio loop touches heartbeat_file every heartbeat_interval_s
    # so an exec liveness probe can restart a pod whose event loop has wedged.
    heartbeat_file: str = Field(
        default="/tmp/agentos-worker.heartbeat",
        validation_alias="AGENTOS_HEARTBEAT_FILE",
    )
    heartbeat_interval_s: float = Field(
        default=10.0, validation_alias="AGENTOS_HEARTBEAT_INTERVAL_SECONDS"
    )

    key_prefix: str = "agentos:worker"

    @property
    def valkey_socket_timeout_s(self) -> float:
        """Socket read timeout for the Valkey clients, kept above the block interval.

        An idle blocking XREADGROUP blocks server-side for ``read_block_ms`` and
        then returns an empty reply, but redis-py enforces the client
        ``socket_timeout`` on that same read (its default is 5s). If the socket
        timeout is not longer than the block, the socket read deadline fires at
        the exact moment the block would return empty, so every idle cycle raises
        a read timeout instead of returning empty and floods the logs. The extra
        headroom past ``read_block_ms`` covers pod-to-pod RTT and Valkey
        processing after the block elapses. Genuine connection blips still raise
        and are logged as real transport errors.
        """
        return self.read_block_ms / 1000 + 5.0

    def done_key(self, slack_event_id: str) -> str:
        return f"{self.key_prefix}:done:{slack_event_id}"

    def side_effect_key(self, slack_event_id: str) -> str:
        return f"{self.key_prefix}:sidefx:{slack_event_id}"

    def lock_key(self, thread_key: str) -> str:
        return f"{self.key_prefix}:lock:{thread_key}"
