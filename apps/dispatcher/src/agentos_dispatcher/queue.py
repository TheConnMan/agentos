"""The queue seam between the dispatcher and the worker (F1).

``QueuedTurn`` (from ``aci_protocol``) is the normalized job the dispatcher
enqueues onto the Valkey Stream and the worker consumes. The model was promoted
into the frozen ACI package (issue #7) so the contract is shared across three
languages and guarded by the schema-compat gate rather than hand-mirrored. This
module owns only the Valkey Stream *transport* of that model (a transport detail
that stays out of the frozen package): the single-``payload`` wire encoding plus
the dedupe guard.

Wire encoding: a Stream entry carries the model as a single ``payload`` field
holding ``model_dump_json()``. A one-field JSON blob keeps the seam explicit and
versionable (add fields without reshaping the Stream schema) and lets the worker
reconstruct the model with ``from_stream_fields``.

Dedupe (idempotency, detailed-architecture 2b rule 5): the idempotency key is the
delivery's ``event_id`` (the Slack event id for the Slack adapter). A retried
delivery must not enqueue twice. We guard with a Valkey ``SET <dedupe_key> 1 NX
EX <ttl>`` before enqueuing: the first delivery claims the key and enqueues; a
retry finds the key present and is dropped. This is chosen over stream-side
dedupe because it is O(1), TTL-bounded (no unbounded dedupe set to prune), and
does not require scanning the Stream.
"""

from typing import TYPE_CHECKING, Any, cast

from aci_protocol import QueuedTurn

from .config import DispatcherConfig

if TYPE_CHECKING:
    from redis import Redis

STREAM_PAYLOAD_FIELD = "payload"


def to_stream_fields(turn: QueuedTurn) -> dict[str, str]:
    """Render a turn to the flat field map an ``XADD`` takes (one ``payload``)."""
    return {STREAM_PAYLOAD_FIELD: turn.model_dump_json()}


def from_stream_fields(fields: dict[str, str]) -> QueuedTurn:
    """Reconstruct a turn from a Stream entry's field map (the worker's entry point)."""
    return QueuedTurn.model_validate_json(fields[STREAM_PAYLOAD_FIELD])


def claim_event(redis_client: "Redis", config: "DispatcherConfig", event_id: str) -> bool:
    """Claim a Slack event id for processing, returning True on the first claim.

    Uses ``SET NX EX`` so exactly one delivery of a given event id wins; retries
    see the key already set and get False, which the caller treats as a duplicate
    to drop.
    """
    claimed = redis_client.set(
        config.dedupe_key(event_id),
        "1",
        nx=True,
        ex=config.dedupe_ttl_seconds,
    )
    return bool(claimed)


def enqueue(redis_client: "Redis", config: "DispatcherConfig", turn: QueuedTurn) -> str:
    """Append the normalized turn to the Valkey Stream, returning its Stream id."""
    # redis-py types the fields param as an invariant dict of a broad key/value
    # union, so a plain dict[str, str] does not match; cast to satisfy the stub.
    fields = cast("dict[Any, Any]", to_stream_fields(turn))
    stream_id = redis_client.xadd(config.stream, fields)
    return _as_str(stream_id)


def _as_str(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value)
