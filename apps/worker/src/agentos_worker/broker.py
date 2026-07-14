"""The stream-broker port (consumer side): the Valkey Stream verbs, behind a type.

Issue #284 / ADR-0027. The queue/stream seam was SOFT — "the redis-py Stream
verbs ARE the port," with `redis.asyncio.Redis` coupled at every call site. This
module draws a thin ``StreamBroker`` Protocol over exactly the consumer-group
verbs the worker's stream plumbing uses, so a future non-redis broker (Redis
Cluster, a managed stream, Kafka/NATS) is a drop-in behind the port rather than a
grep-and-replace of every call site.

Scope discipline:

- **The seam is the non-sacred transport, not the kernel.** ``StreamConsumer``
  (``stream_consumer.py``) holds the group-create / blocking-read / ack loop and
  is routed through this port. The sacred concurrency kernel
  (``kernel.py``/``consumer.py``/``threadlock.py``/``markers.py``) is NOT touched
  — the sacred-module rule forbids it. ``consumer.py``'s ``XAUTOCLAIM``
  crash-recovery call is part of the contract (declared on the Protocol) but it
  keeps calling its inherited ``self._redis`` unchanged.
- **The producer side has its own port**, ``StreamPublisher`` in the dispatcher
  (`apps/dispatcher/.../queue.py`), because the producer is a *sync* redis client
  in a different package. Together they are the broker seam.
- **No second broker is built.** Valkey Streams is the adopted spine (ADR-0007);
  this only makes the eventual swap a drop-in when a real second-broker demand
  arrives.

The verbs are typed permissively (``Any`` payloads/returns) to match the wire
shape the code already casts at each site; ``redis.asyncio.Redis`` structurally
satisfies the Protocol, so it is the one backing today with no adapter.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class StreamBroker(Protocol):
    """The consumer-side stream contract: group semantics + crash recovery.

    A second broker must honor: an ordered stream, consumer groups
    (``xgroup_create`` / ``xreadgroup``), per-entry ack (``xack``), and
    pending-entry reclaim for crash recovery (``xautoclaim``). Dedupe lives on the
    producer side (``StreamPublisher``), beside the stream, not in these verbs.

    The verbs are declared to return a bare ``Awaitable`` (not ``async def``) to
    match how ``redis.asyncio.Redis`` types them, so the real client structurally
    satisfies the port with no adapter; call sites still ``await`` them normally.
    """

    def xgroup_create(
        self, name: Any, groupname: Any, id: Any = ..., mkstream: bool = ...
    ) -> Awaitable[Any]:
        """Create the consumer group (and stream, with ``mkstream``) if absent."""
        ...

    def xreadgroup(
        self,
        groupname: Any,
        consumername: Any,
        streams: Any,
        count: Any = ...,
        block: Any = ...,
    ) -> Awaitable[Any]:
        """Blocking consumer-group read; returns the raw stream/entry structure."""
        ...

    def xack(self, name: Any, groupname: Any, *ids: Any) -> Awaitable[Any]:
        """Acknowledge a handled entry so it leaves the pending-entries list."""
        ...

    def xautoclaim(
        self,
        name: Any,
        groupname: Any,
        consumername: Any,
        min_idle_time: Any,
        start_id: Any = ...,
        count: Any = ...,
        justid: bool = ...,
    ) -> Awaitable[Any]:
        """Reclaim entries pending past ``min_idle_time`` from a dead consumer."""
        ...
