"""Shared fixtures. Stream/dedupe tests run against the REAL Valkey from the
compose stack (per repo test discipline: never mock Valkey). Only the Slack Web
API and socket transport are faked."""

import os
import uuid
from collections.abc import Iterator

import pytest
import redis
from agentos_dispatcher.config import DispatcherConfig

# Compose defaults (compose.dev.yaml maps Valkey to host port 26379, password
# valkeypass). Overridable for CI via TEST_VALKEY_* env vars.
_VALKEY_HOST = os.environ.get("TEST_VALKEY_HOST", "localhost")
_VALKEY_PORT = int(os.environ.get("TEST_VALKEY_PORT", "26379"))
_VALKEY_PW = os.environ.get("TEST_VALKEY_PW", "valkeypass")


@pytest.fixture
def redis_client() -> Iterator[redis.Redis]:
    client = redis.Redis(
        host=_VALKEY_HOST,
        port=_VALKEY_PORT,
        password=_VALKEY_PW or None,
        decode_responses=True,
    )
    try:
        client.ping()
    except redis.exceptions.RedisError as exc:
        pytest.skip(f"Valkey not reachable at {_VALKEY_HOST}:{_VALKEY_PORT}: {exc}")
    yield client
    client.close()


@pytest.fixture
def config(redis_client: redis.Redis) -> Iterator[DispatcherConfig]:
    """A config with a per-test-unique stream and dedupe prefix so tests do not
    collide, cleaned up afterwards."""
    token = uuid.uuid4().hex
    cfg = DispatcherConfig(
        slack_app_token="xapp-test",
        slack_bot_token="xoxb-test",
        valkey_host=_VALKEY_HOST,
        valkey_port=_VALKEY_PORT,
        valkey_password=_VALKEY_PW,
        stream=f"test:agentos:runs:{token}",
        dedupe_prefix=f"test:agentos:dedupe:{token}:",
        dedupe_ttl_seconds=60,
        placeholder_text="Working on it.",
    )
    yield cfg
    keys = list(redis_client.scan_iter(f"test:agentos:dedupe:{token}:*"))
    keys.append(cfg.stream)
    if keys:
        redis_client.delete(*keys)
