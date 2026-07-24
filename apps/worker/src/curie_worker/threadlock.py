"""A Valkey-backed per-thread lock: one live session per thread across workers.

The routing decision (is there a live route? steer it, or open a new turn) plus
the turn opening must be atomic per thread, or two workers racing two events for
the same thread could each open a turn and violate one-live-session-per-thread.
This is a standard single-instance Redis lock: ``SET key token NX PX ttl`` to
acquire, and a Lua compare-and-delete to release only our own token (so a lock
that expired and was re-taken by another worker is never released by us).

The lock is held only for the bounded critical section (decision + turn start),
never for the whole stream, so a follow-up can steer the live turn.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from redis.asyncio import Redis

# Release only if we still own the lock (token match); avoids deleting a lock a
# later holder acquired after ours expired.
_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


class LockAcquireTimeout(TimeoutError):
    """The per-thread lock was not acquired within the configured timeout.

    A ``TimeoutError`` subclass on purpose (#849): it genuinely is a timeout, and
    the kernel's turn path treats a failed turn start as a retryable outcome by
    catching ``TimeoutError`` among the transient errors. As a plain
    ``Exception`` this escaped that catch, so a contended lock left the stream
    entry pending for the whole reclaim window instead of retrying in process.
    Subclassing also makes the turn path uniform with the reset path, where the
    outer ``asyncio.wait_for`` bound already raises ``TimeoutError``. Note that
    since builtin ``TimeoutError`` subclasses ``OSError``, this exception is now
    also an ``OSError``.
    """


class ThreadLock:
    """Acquire/release a per-thread lock keyed in Valkey."""

    def __init__(
        self,
        redis: Redis,
        *,
        ttl_ms: int,
        acquire_timeout_s: float,
        poll_interval_s: float,
    ) -> None:
        self._redis = redis
        self._ttl_ms = ttl_ms
        self._acquire_timeout_s = acquire_timeout_s
        self._poll_interval_s = poll_interval_s

    async def acquire(self, key: str) -> str:
        """Block until the lock is held (returns the owner token) or time out."""
        token = uuid.uuid4().hex
        deadline = time.monotonic() + self._acquire_timeout_s
        while True:
            if await self._redis.set(key, token, nx=True, px=self._ttl_ms):
                return token
            if time.monotonic() >= deadline:
                raise LockAcquireTimeout(key)
            await asyncio.sleep(self._poll_interval_s)

    async def release(self, key: str, token: str) -> None:
        await self._redis.eval(_RELEASE_LUA, 1, key, token)

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        token = await self.acquire(key)
        try:
            yield
        finally:
            await self.release(key, token)
