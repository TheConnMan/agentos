"""Typed configuration for the worker kernel, parsed from the environment.

The kernel needs a Valkey connection (the stream it consumes plus its locks and
markers), a Slack bot token (to edit the placeholder in place), the stream and
consumer-group identity, and the tunables for retry, per-thread locking, and
crash-recovery reclaim. Substrate wiring (namespace, warm pool, runner port)
lives in ``SubstrateConfig`` and is assembled separately by the entrypoint.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def _default_consumer_name() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


class WorkerConfig(BaseModel):
    """Everything the kernel needs, in one typed object."""

    model_config = ConfigDict(frozen=True)

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
    bundle_plugin_dir: str = "/bundles/current"
    default_max_usd_per_day: float = 10.0
    default_max_output_tokens_per_run: int = 100000

    # Runner model + credentials passthrough (injected per-claim into every boot
    # env). fake_model runs the runner's canned FakeModelSession (no Anthropic
    # call, no credential needed) -- the middle-mode default on a laptop.
    # credentials is the opaque AGENTOS_CREDENTIALS the runner forwards to the
    # model call; it is never logged and never required when fake_model is on.
    fake_model: bool = False
    credentials: str = ""

    # When true, clear the Slack assistant-thread status (the "shimmer" the
    # dispatcher set) once a turn ends -- editing the placeholder does not
    # auto-clear it. Off by default; pairs with the dispatcher's AGENTOS_SHIMMER.
    shimmer: bool = False

    # Stream / consumer group (must match the dispatcher's AGENTOS_STREAM)
    stream: str = "agentos:runs"
    consumer_group: str = "agentos-workers"
    consumer_name: str = Field(default_factory=_default_consumer_name)

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
    max_attempts: int = 3
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
    eval_stream: str = "agentos:evals"
    eval_consumer_group: str = "agentos-eval-workers"
    eval_consumer_name: str = Field(default_factory=_default_consumer_name)
    # MinIO / S3 for plugin bundles (mirrors the API's env names). The consumer
    # fetches a version's bundle by bundle_ref and loads its evals/ suite.
    s3_endpoint_url: str = "http://localhost:29000"
    s3_access_key: str = "minio"
    s3_secret_key: str = "miniosecret"
    s3_region: str = "us-east-1"
    bundle_bucket: str = "agentos-bundles"
    # Platform API for POST /evals/report. Defaults match the API's dev stack
    # (README serves it on :8000; its shared dev key is agentos-dev-key).
    api_base_url: str = "http://localhost:8000"
    api_key: str = "agentos-dev-key"
    report_max_attempts: int = 3
    report_backoff_base_s: float = Field(default=0.5, gt=0)
    # Langfuse for recording eval scores (the matrix reads them back by version).
    langfuse_host: str = "http://localhost:23000"
    langfuse_public_key: str = "pk-lf-agentos-dev"
    langfuse_secret_key: str = "sk-lf-agentos-dev"

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

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> WorkerConfig:
        values: dict[str, Any] = {}
        _s(values, "valkey_host", env, "VALKEY_HOST")
        _i(values, "valkey_port", env, "VALKEY_PORT")
        _s(values, "valkey_password", env, "VALKEY_PASSWORD")
        _i(values, "valkey_db", env, "VALKEY_DB")
        _s(values, "slack_bot_token", env, "SLACK_BOT_TOKEN")
        _s(values, "slack_api_base_url", env, "SLACK_API_BASE_URL")
        _s(values, "database_url", env, "DATABASE_URL")
        _s(values, "db_schema", env, "DB_SCHEMA")
        _s(values, "bundle_plugin_dir", env, "AGENTOS_PLUGIN_DIR")
        _b(values, "fake_model", env, "AGENTOS_FAKE_MODEL")
        _b(values, "shimmer", env, "AGENTOS_SHIMMER")
        _s(values, "credentials", env, "AGENTOS_CREDENTIALS")
        _s(values, "eval_stream", env, "AGENTOS_EVAL_STREAM")
        _s(values, "eval_consumer_group", env, "AGENTOS_EVAL_CONSUMER_GROUP")
        _s(values, "s3_endpoint_url", env, "S3_ENDPOINT_URL")
        _s(values, "s3_access_key", env, "S3_ACCESS_KEY")
        _s(values, "s3_secret_key", env, "S3_SECRET_KEY")
        _s(values, "s3_region", env, "S3_REGION")
        _s(values, "bundle_bucket", env, "BUNDLE_BUCKET")
        _s(values, "api_base_url", env, "AGENTOS_API_BASE_URL")
        _s(values, "api_key", env, "AGENTOS_API_KEY")
        _s(values, "langfuse_host", env, "LANGFUSE_HOST")
        _s(values, "langfuse_public_key", env, "LANGFUSE_PUBLIC_KEY")
        _s(values, "langfuse_secret_key", env, "LANGFUSE_SECRET_KEY")
        _s(values, "stream", env, "AGENTOS_STREAM")
        _s(values, "consumer_group", env, "AGENTOS_CONSUMER_GROUP")
        _s(values, "consumer_name", env, "AGENTOS_CONSUMER_NAME")
        _i(values, "max_attempts", env, "AGENTOS_MAX_ATTEMPTS")
        return cls(**values)


def _s(values: dict[str, Any], key: str, env: Mapping[str, str], var: str) -> None:
    if var in env:
        values[key] = env[var]


def _i(values: dict[str, Any], key: str, env: Mapping[str, str], var: str) -> None:
    if var in env:
        values[key] = int(env[var])


def _b(values: dict[str, Any], key: str, env: Mapping[str, str], var: str) -> None:
    if var in env:
        values[key] = env[var].strip().lower() in ("1", "true", "yes")
