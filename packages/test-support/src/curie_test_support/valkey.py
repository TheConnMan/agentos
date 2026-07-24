"""Shared Valkey connect-and-skip helpers for the test suites.

The three constants read the same env vars with the same compose defaults every
test site used before consolidation (compose.dev.yaml maps Valkey to host port
26379, password ``valkeypass``); ``connect_or_skip`` is the sync build+ping+skip
block those sites duplicated.
"""

from __future__ import annotations

import os

import pytest
import redis

VALKEY_HOST = os.environ.get("TEST_VALKEY_HOST", "localhost")
VALKEY_PORT = int(os.environ.get("TEST_VALKEY_PORT", "26379"))
VALKEY_PW = os.environ.get("TEST_VALKEY_PW", "valkeypass")


def connect_or_skip(*, decode_responses: bool = True) -> redis.Redis:
    """A sync Valkey client against the compose stack, or ``pytest.skip``.

    Builds a ``redis.Redis`` on the shared ``TEST_VALKEY_*`` connection params,
    pings it, and skips the test when Valkey is unreachable. The caller owns the
    returned client (yield it from a fixture and ``.close()`` on teardown).
    """
    client: redis.Redis = redis.Redis(
        host=VALKEY_HOST,
        port=VALKEY_PORT,
        password=VALKEY_PW or None,
        decode_responses=decode_responses,
    )
    try:
        client.ping()
    except redis.exceptions.RedisError as exc:
        pytest.skip(f"Valkey not reachable at {VALKEY_HOST}:{VALKEY_PORT}: {exc}")
    return client
