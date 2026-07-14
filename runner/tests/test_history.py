"""The conversation-history port: resolution, turn shape, preamble, the state-API
store, and the per-turn append that persists a thread's transcript (#20).

The StateApiTranscriptStore is exercised against a tiny in-memory fake of the
#248 log-shaped state endpoints (GET the key, POST .../append), so load/append
round-trip over real HTTP without the API. The write side is exercised by driving
a real turn through the SessionRunner with the fake model.
"""

import anyio
import pytest
from agentos_runner.history import (
    HistoryError,
    NullTranscriptStore,
    StateApiTranscriptStore,
    TranscriptStore,
    TurnRecord,
    format_conversation_preamble,
    resolve_history,
)
from aiohttp import web
from aiohttp.test_utils import TestServer


def _fake_state_app() -> tuple[web.Application, list]:
    """A minimal fake of the state key at /agents/A/state/transcript/t1."""
    log: list = []
    app = web.Application()
    key = "/agents/A/state/transcript/t1"

    async def get_key(request: web.Request) -> web.Response:
        if not log:
            return web.json_response({"detail": "not found"}, status=404)
        return web.json_response(
            {"namespace": "transcript", "key": "t1", "value": list(log), "version": len(log)}
        )

    async def append_key(request: web.Request) -> web.Response:
        body = await request.json()
        log.append(body["item"])
        return web.json_response(
            {"namespace": "transcript", "key": "t1", "value": list(log), "version": len(log)}
        )

    app.router.add_get(key, get_key)
    app.router.add_post(f"{key}/append", append_key)
    return app, log


def test_turn_record_round_trip() -> None:
    rec = TurnRecord(
        user="what changed?", assistant="the deploy bumped v3", ts="2026-07-14T00:00:00+00:00"
    )
    assert TurnRecord.from_dict(rec.to_dict()) == rec


def test_resolve_absent_ref_is_null_store() -> None:
    store = resolve_history(None, {})
    assert isinstance(store, NullTranscriptStore)
    assert anyio.run(store.load) == []
    # Append on the null store is a silent no-op.
    anyio.run(store.append, TurnRecord(user="u", assistant="a"))


def test_resolve_http_ref_is_state_store() -> None:
    store = resolve_history("http://api:8000/agents/A/state/transcript/t1", {})
    assert isinstance(store, StateApiTranscriptStore)


def test_resolve_unsupported_scheme_raises() -> None:
    # An old SDK-resume id (or any non-http ref) is rejected loudly, not silently
    # dropped, so a misconfigured ref fails visibly at boot.
    with pytest.raises(HistoryError):
        resolve_history("sdk-session-abc123", {})
    with pytest.raises(HistoryError):
        resolve_history("s3://bucket/hist", {})


def test_preamble_empty_is_none() -> None:
    assert format_conversation_preamble([]) is None


def test_preamble_includes_user_and_assistant_text_oldest_first() -> None:
    turns = [
        TurnRecord(user="deploy the app", assistant="pushed to dev"),
        TurnRecord(user="and prod?", assistant="promoted to prod"),
    ]
    preamble = format_conversation_preamble(turns)
    assert preamble is not None
    assert "deploy the app" in preamble
    assert "pushed to dev" in preamble
    assert "and prod?" in preamble
    assert "promoted to prod" in preamble
    # Oldest first: the first turn's user text precedes the second turn's.
    assert preamble.index("deploy the app") < preamble.index("and prod?")


def test_state_store_load_empty_is_empty() -> None:
    app, _ = _fake_state_app()

    async def go() -> None:
        async with TestServer(app) as server:
            url = str(server.make_url("/agents/A/state/transcript/t1"))
            store = StateApiTranscriptStore(url, token=None)
            assert await store.load() == []

    anyio.run(go)


def test_state_store_append_then_load_round_trip() -> None:
    app, log = _fake_state_app()

    async def go() -> None:
        async with TestServer(app) as server:
            url = str(server.make_url("/agents/A/state/transcript/t1"))
            store = StateApiTranscriptStore(url, token="k")
            await store.append(
                TurnRecord(user="q1", assistant="a1", ts="2026-07-14T00:00:00+00:00")
            )
            await store.append(TurnRecord(user="q2", assistant="a2"))
            loaded = await store.load()
            assert loaded == [
                TurnRecord(user="q1", assistant="a1", ts="2026-07-14T00:00:00+00:00"),
                TurnRecord(user="q2", assistant="a2"),
            ]
            assert len(log) == 2

    anyio.run(go)


def test_state_store_load_rejects_non_array() -> None:
    app = web.Application()

    async def get_key(_request: web.Request) -> web.Response:
        return web.json_response(
            {"namespace": "transcript", "key": "t1", "value": {"not": "a list"}, "version": 1}
        )

    app.router.add_get("/agents/A/state/transcript/t1", get_key)

    async def go() -> None:
        async with TestServer(app) as server:
            url = str(server.make_url("/agents/A/state/transcript/t1"))
            store = StateApiTranscriptStore(url, token=None)
            with pytest.raises(HistoryError):
                await store.load()

    anyio.run(go)


def _recording_runner(store: TranscriptStore):
    """A SessionRunner wired to the fake model and a recording transcript store."""
    from agentos_runner import RunTracer, SideEffectClassifier
    from agentos_runner.fake import FakeModelSession, default_turn
    from agentos_runner.session import SessionRunner

    return SessionRunner(
        session_factory=lambda: FakeModelSession(default_turn),
        ceiling=0,
        tracer=RunTracer(None),
        classifier=SideEffectClassifier(),
        trace_name="t",
        session_id="sess-hist",
        history_store=store,
    )


class _RecordingStore:
    def __init__(self) -> None:
        self.turns: list[TurnRecord] = []

    async def load(self) -> list[TurnRecord]:
        return list(self.turns)

    async def append(self, record: TurnRecord) -> None:
        self.turns.append(record)


def test_successful_turn_is_appended_to_the_transcript() -> None:
    from aci_protocol import Event

    store = _RecordingStore()
    runner = _recording_runner(store)

    async def go() -> None:
        await runner.start()
        async for _line in runner.run_inbound(
            Event(type="message", text="what changed?", user="U", ts="1")
        ):
            pass

    anyio.run(go)

    assert len(store.turns) == 1
    assert store.turns[0].user == "what changed?"
    # default_turn's terminal result text.
    assert store.turns[0].assistant == "all done"
    assert store.turns[0].ts  # a timestamp was stamped


def test_failed_turn_is_not_appended() -> None:
    # A turn that never produced a successful terminal final (final_text stays
    # None) must not be recorded, so the transcript holds only delivered answers.
    from aci_protocol import Event
    from agentos_runner import RunTracer, SideEffectClassifier
    from agentos_runner.session import SessionRunner
    from agentos_runner.translate import TurnState

    store = _RecordingStore()
    runner = SessionRunner(
        session_factory=lambda: None,  # type: ignore[arg-type,return-value]
        ceiling=0,
        tracer=RunTracer(None),
        classifier=SideEffectClassifier(),
        trace_name="t",
        session_id="s",
        history_store=store,
    )
    event = Event(type="message", text="q", user="U", ts="1")

    # final_text None (a failed/aborted turn) -> no append.
    anyio.run(lambda: runner._record_turn(event, TurnState()))
    assert store.turns == []

    # final_text set (a delivered answer) -> appended.
    state = TurnState()
    state.final_text = "the answer"
    anyio.run(lambda: runner._record_turn(event, state))
    assert len(store.turns) == 1
    assert store.turns[0].assistant == "the answer"


def test_compose_system_prompt_orders_memory_then_conversation_then_base() -> None:
    # Boot delivery (ADR-0029): durable memory leads, then this thread's recovered
    # conversation, then the bundle/env system prompt. Any part may be absent.
    from agentos_runner.__main__ import _compose_system_prompt

    assert _compose_system_prompt("BASE", "MEM", "CONV") == "MEM\n\nCONV\n\nBASE"
    assert _compose_system_prompt("BASE", None, "CONV") == "CONV\n\nBASE"
    assert _compose_system_prompt("BASE", "MEM", None) == "MEM\n\nBASE"
    assert _compose_system_prompt(None, None, None) is None


def test_record_turn_swallows_store_failure() -> None:
    # A transient store failure must never fail a turn the user already answered.
    from aci_protocol import Event
    from agentos_runner import RunTracer, SideEffectClassifier
    from agentos_runner.session import SessionRunner
    from agentos_runner.translate import TurnState

    class _BoomStore:
        async def load(self) -> list[TurnRecord]:
            return []

        async def append(self, record: TurnRecord) -> None:
            raise HistoryError("state API unavailable")

    runner = SessionRunner(
        session_factory=lambda: None,  # type: ignore[arg-type,return-value]
        ceiling=0,
        tracer=RunTracer(None),
        classifier=SideEffectClassifier(),
        trace_name="t",
        session_id="s",
        history_store=_BoomStore(),
    )
    state = TurnState()
    state.final_text = "answer"
    # Must not raise.
    anyio.run(lambda: runner._record_turn(Event(type="message", text="q", user="U", ts="1"), state))
