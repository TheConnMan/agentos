"""KillSwitch consumer against the REAL compose Valkey (never mocked).

Publishes on the L1 channel and sets/clears the flag key exactly as the API does,
and checks: is_killed reflects the flag; the subscriber invokes on_kill for a
kill event and covers a missed pubsub via the flag; a resume needs no worker
action.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import Callable

import pytest
import redis.asyncio as aredis
from agentos_worker.killswitch import KILL_CHANNEL, KillSwitch, kill_key

_VALKEY_HOST = os.environ.get("TEST_VALKEY_HOST", "localhost")
_VALKEY_PORT = int(os.environ.get("TEST_VALKEY_PORT", "26379"))
_VALKEY_PW = os.environ.get("TEST_VALKEY_PW", "valkeypass")


def _client() -> aredis.Redis:
    return aredis.Redis(
        host=_VALKEY_HOST, port=_VALKEY_PORT, password=_VALKEY_PW or None, decode_responses=True
    )


async def _wait_until(pred: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


def test_is_killed_reflects_the_flag_key() -> None:
    async def go() -> None:
        client = _client()
        try:
            await client.ping()
        except aredis.RedisError as exc:
            pytest.skip(f"Valkey not reachable: {exc}")
        agent_id = uuid.uuid4()
        ks = KillSwitch(client, on_kill=_noop)
        try:
            assert await ks.is_killed(agent_id) is False
            await client.set(kill_key(agent_id), "1")
            assert await ks.is_killed(agent_id) is True  # covers a missed pubsub
            await client.delete(kill_key(agent_id))
            assert await ks.is_killed(agent_id) is False
        finally:
            await client.aclose()

    asyncio.run(go())


def test_subscriber_invokes_on_kill_for_a_kill_event() -> None:
    async def go() -> None:
        client = _client()
        publisher = _client()
        try:
            await client.ping()
        except aredis.RedisError as exc:
            pytest.skip(f"Valkey not reachable: {exc}")

        killed: list[uuid.UUID] = []

        async def on_kill(agent_id: uuid.UUID) -> None:
            killed.append(agent_id)

        ks = KillSwitch(client, on_kill=on_kill)
        task = asyncio.create_task(ks.run())
        try:
            # Give the subscriber a moment to subscribe before publishing.
            await asyncio.sleep(0.2)
            agent_id = uuid.uuid4()
            await publisher.publish(
                KILL_CHANNEL,
                json.dumps({"agent_id": str(agent_id), "action": "kill", "ts": "now"}),
            )
            await _wait_until(lambda: bool(killed))
            assert killed == [agent_id]

            # A resume event triggers no interrupt callback.
            await publisher.publish(
                KILL_CHANNEL,
                json.dumps({"agent_id": str(uuid.uuid4()), "action": "resume", "ts": "now"}),
            )
            await asyncio.sleep(0.3)
            assert len(killed) == 1
        finally:
            ks.request_stop()
            await task
            await client.aclose()
            await publisher.aclose()

    asyncio.run(go())


async def _noop(_agent_id: uuid.UUID) -> None:
    return None
