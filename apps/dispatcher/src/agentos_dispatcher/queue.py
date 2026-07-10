"""The queue seam between the dispatcher and the worker (F1).

``QueuedSlackEvent`` is the normalized job the dispatcher enqueues onto the
Valkey Stream and the worker consumes. The *shape* was promoted into the frozen
tri-language contract ``aci_protocol`` (#7) and is now the single source of truth
generated into Rust and TypeScript. The dispatcher-side subclass below adds the
Valkey Stream transport helpers (``to_stream_fields``/``from_stream_fields``),
which are producer/consumer plumbing, not part of the cross-language data shape.

Wire encoding: a Stream entry carries the model as a single ``payload`` field
holding ``model_dump_json()``. A one-field JSON blob keeps the seam explicit and
versionable (add fields without reshaping the Stream schema) and lets the worker
reconstruct the model with ``from_stream_fields``.

Dedupe (idempotency, detailed-architecture 2b rule 5): the idempotency key is the
Slack event id. A retried Slack delivery must not enqueue twice. We guard with a
Valkey ``SET <dedupe_key> 1 NX EX <ttl>`` before enqueuing: the first delivery
claims the key and enqueues; a retry finds the key present and is dropped. This is
chosen over stream-side dedupe because it is O(1), TTL-bounded (no unbounded
dedupe set to prune), and does not require scanning the Stream.
"""

from typing import TYPE_CHECKING, Any, cast

from aci_protocol import QueuedSlackEvent as _QueuedSlackEvent

from .config import DispatcherConfig

if TYPE_CHECKING:
    from redis import Redis

STREAM_PAYLOAD_FIELD = "payload"


class QueuedSlackEvent(_QueuedSlackEvent):
    """The promoted ``aci_protocol.QueuedSlackEvent`` shape plus the Valkey Stream
    transport helpers. The field shape (and its cross-language generation) lives
    in ``aci_protocol`` (#7); only the stream (de)serialization is dispatcher-side.
    """

    def to_stream_fields(self) -> dict[str, str]:
        """Render to the flat field map an ``XADD`` takes."""
        return {STREAM_PAYLOAD_FIELD: self.model_dump_json()}

    @classmethod
    def from_stream_fields(cls, fields: dict[str, str]) -> "QueuedSlackEvent":
        """Reconstruct from a Stream entry's field map (the worker's entry point)."""
        return cls.model_validate_json(fields[STREAM_PAYLOAD_FIELD])


def claim_event(redis_client: "Redis", config: "DispatcherConfig", slack_event_id: str) -> bool:
    """Claim a Slack event id for processing, returning True on the first claim.

    Uses ``SET NX EX`` so exactly one delivery of a given event id wins; retries
    see the key already set and get False, which the caller treats as a duplicate
    to drop.
    """
    claimed = redis_client.set(
        config.dedupe_key(slack_event_id),
        "1",
        nx=True,
        ex=config.dedupe_ttl_seconds,
    )
    return bool(claimed)


def enqueue(redis_client: "Redis", config: "DispatcherConfig", event: QueuedSlackEvent) -> str:
    """Append the normalized event to the Valkey Stream, returning its Stream id."""
    # redis-py types the fields param as an invariant dict of a broad key/value
    # union, so a plain dict[str, str] does not match; cast to satisfy the stub.
    fields = cast("dict[Any, Any]", event.to_stream_fields())
    stream_id = redis_client.xadd(config.stream, fields)
    return _as_str(stream_id)


def _as_str(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value)
