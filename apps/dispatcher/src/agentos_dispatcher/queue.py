"""The queue seam between the dispatcher and the worker (F1).

``QueuedSlackEvent`` is the normalized job the dispatcher enqueues onto the
Valkey Stream and the worker consumes. It is defined here, inside the dispatcher,
because the dispatcher is the producer and therefore owns the shape; F1 imports
or mirrors this model. If it later deserves promotion into a shared package, the
orchestrator makes that call (dispatcher does not touch ``packages/``).

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

from pydantic import BaseModel

from .config import DispatcherConfig

if TYPE_CHECKING:
    from redis import Redis

STREAM_PAYLOAD_FIELD = "payload"


class QueuedSlackEvent(BaseModel):
    """A normalized Slack event ready for the worker to route and run.

    Fields:
        slack_event_id: Slack's per-delivery event id; the idempotency key.
        thread_ts: the canonical thread key (the root message ts of the thread).
        channel: Slack channel id the message arrived in.
        user: Slack user id that authored the message.
        text: the message text.
        placeholder_ts: ts of the placeholder reply the dispatcher already posted;
            the worker edits this message in place with the real response.
        received_at: ISO-8601 UTC timestamp of when the dispatcher received it.
    """

    slack_event_id: str
    thread_ts: str
    channel: str
    user: str
    text: str
    placeholder_ts: str
    received_at: str

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
