"""Dispatch-level isolation in the shared consume loop (#673).

A handler-internal error (the realistic trigger: a transient transport fault
during a poison-pill dead-letter XADD) must not escape ``_consume``. If it did,
it would propagate out of the loop and -- because every consumer shares one event
loop under the top-level gather -- tear its siblings (runs, evals, killswitch,
heartbeat) down. The loop must instead log-and-continue, leaving the entry
un-acked in the PEL for the reclaim loop.
"""

from __future__ import annotations

import asyncio
import logging

from curie_worker.stream_consumer import ReadLoopSpec, StreamConsumer


class _FakeBroker:
    """Returns one batch of entries, then drains (requesting stop) so the loop
    terminates. Only the two verbs ``_consume`` touches are implemented."""

    def __init__(self, batch: list[tuple[str, dict[str, str]]], on_drained) -> None:  # noqa: ANN001
        # xreadgroup returns list[(stream, entries)]; hand back one stream's worth.
        self._responses: list[object] = [[("s", batch)]]
        self._on_drained = on_drained

    async def xgroup_create(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
        return True

    async def xreadgroup(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
        if self._responses:
            return self._responses.pop(0)
        self._on_drained()  # no more work: ask the loop to stop
        return []


def _spec(logger: logging.Logger) -> ReadLoopSpec:
    return ReadLoopSpec(
        stream="s",
        group="g",
        consumer="c",
        count=10,
        block_ms=5,
        backoff_s=0.0,
        timeout_msg="timeout: %s",
        connection_msg="connection: %s",
        logger=logger,
    )


def test_handler_exception_does_not_tear_down_the_consume_loop(caplog) -> None:  # noqa: ANN001
    """A handler that raises on the middle entry still lets the loop process the
    entries before and after it, and the loop returns normally (no propagation)."""

    async def go() -> None:
        handled: list[str] = []

        async def handler(entry_id: str, _fields: dict[str, str]) -> None:
            handled.append(entry_id)
            if entry_id == "2-0":
                raise ConnectionError("dead-letter XADD hit a Valkey blip")

        batch = [("1-0", {}), ("2-0", {}), ("3-0", {})]
        # The broker asks the loop to stop once its one batch is drained; wire the
        # callback to the consumer after construction to avoid a chicken-and-egg.
        holder: dict[str, StreamConsumer] = {}
        broker = _FakeBroker(batch, lambda: holder["c"].request_stop())
        consumer = StreamConsumer(broker)  # type: ignore[arg-type]
        holder["c"] = consumer

        logger = logging.getLogger("curie_worker.test_consume")
        with caplog.at_level(logging.ERROR, logger=logger.name):
            # Must not raise -- the ConnectionError on "2-0" is isolated.
            await asyncio.wait_for(consumer._consume(_spec(logger), handler), timeout=2)

        # The sibling entries on either side of the failing one were both handled:
        # the exception did not abort the batch or the loop.
        assert handled == ["1-0", "2-0", "3-0"]

        # The failure was logged with a traceback (exception-level), naming the entry.
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "2-0" in errors[0].getMessage()
        assert errors[0].exc_info is not None

    asyncio.run(go())
