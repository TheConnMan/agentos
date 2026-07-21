"""Force a fresh sandbox for one stuck thread (#713).

A thread's sandbox binds whatever env it booted with for its entire life (model
credential, Slack wiring, bundle version) -- the worker only re-derives env for
a *new* claim, never for one already adopted by a live route. When that env
goes stale (a rotated credential, a bundle redeploy the thread hasn't picked
up, a sandbox wedged after a partial local-stack upgrade), the only way to
force a cold-create today is to reach into Kubernetes/Docker and the Valkey
route key by hand.

This is a lightweight signal, not a pub/sub channel like the kill switch
(``killswitch.py``): a thread-reset request is a one-shot administrative
action with no live "is this still requested" state to gate a running turn on,
so a Valkey SET the worker's existing maintenance tick drains is enough -- no
new subscriber process, no new lifecycle to manage. ``THREAD_RESET_SET`` is
duplicated verbatim in ``apps/worker/src/agentos_worker/consumer.py`` (the
worker's own copy), the same cross-service-constant pattern the kill switch
already uses (`apps/worker/src/agentos_worker/killswitch.py`'s
``KILL_KEY_PREFIX``/``KILL_CHANNEL`` mirror this module's) since neither
service imports the other's package.
"""

import redis.asyncio as redis

THREAD_RESET_SET = "agentos:thread-reset-requests"


class ThreadResetRequests:
    """Requests (from the API) and drains (from the worker) pending thread
    keys whose sandbox should be force-released on the next maintenance tick."""

    def __init__(self, client: redis.Redis) -> None:
        self._client = client

    async def request(self, thread_key: str) -> None:
        """Queue ``thread_key`` for a forced sandbox release. Idempotent --
        adding an already-pending thread is a no-op (a Valkey SET member)."""
        await self._client.sadd(THREAD_RESET_SET, thread_key)

    async def is_pending(self, thread_key: str) -> bool:
        return bool(await self._client.sismember(THREAD_RESET_SET, thread_key))
