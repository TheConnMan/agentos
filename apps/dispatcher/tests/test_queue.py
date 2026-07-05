"""Queue seam + dedupe against real Valkey."""

import redis
from agentos_dispatcher.config import DispatcherConfig
from agentos_dispatcher.queue import (
    QueuedSlackEvent,
    claim_event,
    enqueue,
)


def _event(event_id: str = "Ev1") -> QueuedSlackEvent:
    return QueuedSlackEvent(
        slack_event_id=event_id,
        thread_ts="123.45",
        channel="C1",
        user="U1",
        text="hello",
        placeholder_ts="999.00",
        received_at="2026-07-05T00:00:00+00:00",
    )


def test_queued_event_stream_fields_roundtrip() -> None:
    event = _event()
    fields = event.to_stream_fields()
    # The worker (F1) consumes a single JSON payload field.
    assert set(fields) == {"payload"}
    assert QueuedSlackEvent.from_stream_fields(fields) == event


def test_enqueue_writes_one_stream_entry_the_worker_can_read(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    event = _event()
    stream_id = enqueue(redis_client, config, event)

    assert redis_client.xlen(config.stream) == 1
    entries = redis_client.xrange(config.stream)
    entry_id, fields = entries[0]
    assert entry_id == stream_id
    # Round-trips through the wire back into the model the worker reconstructs.
    assert QueuedSlackEvent.from_stream_fields(fields) == event


def test_claim_event_is_first_writer_wins(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    assert claim_event(redis_client, config, "Ev-dedupe") is True
    # A second (retried) delivery of the same event id is rejected.
    assert claim_event(redis_client, config, "Ev-dedupe") is False
    # A different event id is unaffected.
    assert claim_event(redis_client, config, "Ev-other") is True


def test_claim_event_sets_a_ttl_so_the_guard_is_bounded(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    claim_event(redis_client, config, "Ev-ttl")
    ttl = redis_client.ttl(config.dedupe_key("Ev-ttl"))
    assert 0 < ttl <= config.dedupe_ttl_seconds
