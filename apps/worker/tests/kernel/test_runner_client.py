"""Regression guard for the RunnerClient turn-stream release contract that the
kernel's _consume relies on (verify-f1 coverage gap 1): the aiohttp response must
be released on every exit path -- normal completion and an exception mid-stream --
so a turn never leaks a connection. We spy on the response's release() because it
is what TurnStream.close (called from __aexit__) invokes."""

from __future__ import annotations

import asyncio
from typing import Any

from aci_protocol import Event, Final, SessionStatus, TextDelta
from agentos_worker.runner_client import RunnerClient

DONE = SessionStatus.DONE


def _event() -> Event:
    return Event(type="message", text="hi", user="U", ts="1")


def _spy_release(turn: Any) -> dict[str, int]:
    calls = {"n": 0}
    real = turn._response.release

    def spy() -> Any:
        calls["n"] += 1
        return real()

    turn._response.release = spy
    return calls


def test_turn_stream_released_on_normal_completion(make_harness) -> None:
    async def go() -> None:
        async with make_harness() as h:
            h.runner.default_script = [TextDelta(text="x"), Final(text="done", status=DONE)]
            handle = await asyncio.to_thread(h.substrate.claim, "tS")
            client = RunnerClient(total_timeout_s=30.0)
            try:
                turn = await client.start_turn(handle.base_url, _event())
                calls = _spy_release(turn)
                async with turn:
                    async for _frame in turn:
                        pass
                assert calls["n"] >= 1  # released on normal exit
            finally:
                await client.close()

    asyncio.run(go())


def test_turn_stream_released_when_consumer_raises(make_harness) -> None:
    async def go() -> None:
        async with make_harness() as h:
            # A hanging turn: the body is not fully read, so aiohttp will not
            # auto-release on EOF -- only TurnStream.__aexit__ can release it.
            hold = asyncio.Event()
            h.runner.hold = hold
            h.runner.default_script = [TextDelta(text="x")]
            h.runner.tail = [Final(text="done", status=DONE)]
            handle = await asyncio.to_thread(h.substrate.claim, "tSraise")
            client = RunnerClient(total_timeout_s=30.0)
            try:
                turn = await client.start_turn(handle.base_url, _event())
                calls = _spy_release(turn)
                try:
                    async with turn:
                        raise RuntimeError("consumer blew up mid-stream")
                except RuntimeError:
                    pass
                assert calls["n"] >= 1  # released on the error path too
            finally:
                hold.set()
                await client.close()

    asyncio.run(go())
