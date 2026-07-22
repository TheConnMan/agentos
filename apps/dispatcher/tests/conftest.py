"""Shared fixtures. Stream/dedupe tests run against the REAL Valkey from the
compose stack (per repo test discipline: never mock Valkey). Only the Slack Web
API and socket transport are faked."""

import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import redis
from agentos_dispatcher.config import DispatcherConfig
from agentos_test_support.valkey import (
    VALKEY_HOST as _VALKEY_HOST,
)
from agentos_test_support.valkey import (
    VALKEY_PORT as _VALKEY_PORT,
)
from agentos_test_support.valkey import (
    VALKEY_PW as _VALKEY_PW,
)
from agentos_test_support.valkey import (
    connect_or_skip,
)
from slack_bolt.authorization import AuthorizeResult


def _authorize(**_kwargs: Any) -> AuthorizeResult:
    """Shared authorization stub: Bolt's ``authorize`` callback resolved to a
    fixed bot identity, so Socket Mode tests skip the real auth.test call."""
    return AuthorizeResult(
        enterprise_id=None,
        team_id="T1",
        bot_token="xoxb-test",
        bot_id="B1",
        bot_user_id="U0BOT",
    )


class FakeSocketClient:
    """Captures the envelope acks Bolt sends back over the socket."""

    def __init__(self) -> None:
        self.logger = logging.getLogger("fake-socket")
        self.acked_envelope_ids: list[str] = []

    def send_socket_mode_response(self, response: Any) -> None:
        self.acked_envelope_ids.append(response.envelope_id)

# Compose defaults and connection params come from the shared agentos_test_support.valkey helper.


@pytest.fixture
def redis_client() -> Iterator[redis.Redis]:
    client = connect_or_skip(decode_responses=True)
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
