"""Worker slice of the L1 kill switch.

The API publishes on the Valkey channel ``agentos:kill-events`` a JSON
``{agent_id, action, ts}`` and sets/clears a flag key ``agentos:kill:<agent_id>``
(no TTL). This consumer subscribes to the channel and, on a ``kill``, interrupts
that agent's live turns within seconds via a supplied callback. New runs are
gated separately by ``is_killed`` (a direct flag-key check the kernel does before
opening a turn), which also covers a kill event missed while the subscriber was
down.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

KILL_CHANNEL = "agentos:kill-events"
KILL_KEY_PREFIX = "agentos:kill:"


def kill_key(agent_id: uuid.UUID) -> str:
    return f"{KILL_KEY_PREFIX}{agent_id}"


class KillSwitch:
    """Subscribes to kill events and gates/interrupts runs for killed agents."""

    def __init__(
        self,
        redis: Redis,
        *,
        on_kill: Callable[[uuid.UUID], Awaitable[object]],
    ) -> None:
        self._redis = redis
        self._on_kill = on_kill
        self._stop = asyncio.Event()

    async def is_killed(self, agent_id: uuid.UUID) -> bool:
        """True if the agent's kill flag is set. Checked before opening a turn so
        a missed pubsub message still refuses new runs."""
        return bool(await self._redis.exists(kill_key(agent_id)))

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """Subscribe and dispatch kill events until asked to stop."""
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(KILL_CHANNEL)
        try:
            while not self._stop.is_set():
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message is None:
                    continue
                await self._handle(message)
        finally:
            await pubsub.unsubscribe(KILL_CHANNEL)
            await pubsub.aclose()  # type: ignore[no-untyped-call]

    async def _handle(self, message: dict[str, object]) -> None:
        try:
            payload = json.loads(_as_text(message["data"]))
            action = payload["action"]
            agent_id = uuid.UUID(payload["agent_id"])
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            logger.exception("malformed kill event: %r", message.get("data"))
            return
        # A resume needs no worker action: the flag is already cleared by the API,
        # so is_killed lets new runs through. Only a kill interrupts live turns.
        # Guard on_kill so a failed interrupt of one agent does not tear down the
        # subscriber and miss every later kill event.
        if action == "kill":
            try:
                await self._on_kill(agent_id)
            except Exception:
                logger.exception("kill handler failed for agent %s", agent_id)


def _as_text(data: object) -> str:
    if isinstance(data, bytes):
        return data.decode("utf-8")
    return str(data)
