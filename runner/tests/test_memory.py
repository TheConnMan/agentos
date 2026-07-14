"""The memory port: resolution, record shape, preamble, and the state-API store.

The StateApiMemoryStore is exercised against a tiny in-memory fake of the #248
log-shaped state endpoints (GET the key, POST .../append), so load/append and the
provenance round-trip are verified end-to-end over real HTTP without the API.
"""

import anyio
import pytest
from agentos_runner.memory import (
    MemoryError,
    MemoryRecord,
    NullMemoryStore,
    Provenance,
    StateApiMemoryStore,
    format_memory_preamble,
    resolve_memory,
)
from aiohttp import web
from aiohttp.test_utils import TestServer


def _fake_state_app() -> tuple[web.Application, list]:
    """A minimal fake of the state API's log key at /agents/A/state/memory/log."""
    log: list = []
    app = web.Application()

    async def get_log(request: web.Request) -> web.Response:
        if not log:
            return web.json_response({"detail": "not found"}, status=404)
        return web.json_response(
            {"namespace": "memory", "key": "log", "value": list(log), "version": len(log)}
        )

    async def append_log(request: web.Request) -> web.Response:
        body = await request.json()
        log.append(body["item"])
        return web.json_response(
            {"namespace": "memory", "key": "log", "value": list(log), "version": len(log)}
        )

    app.router.add_get("/agents/A/state/memory/log", get_log)
    app.router.add_post("/agents/A/state/memory/log/append", append_log)
    return app, log


def test_record_and_provenance_round_trip() -> None:
    rec = MemoryRecord(
        content="prefer ruff over flake8",
        provenance=Provenance(
            learned_from_session_id="sess-1",
            source_trace_ids=("trace-a", "trace-b"),
            recorded_at="2026-07-13T00:00:00+00:00",
        ),
    )
    restored = MemoryRecord.from_dict(rec.to_dict())
    assert restored == rec


def test_resolve_absent_ref_is_null_store() -> None:
    store = resolve_memory(None, {})
    assert isinstance(store, NullMemoryStore)
    assert anyio.run(store.load) == []
    # Append on the null store is a silent no-op.
    anyio.run(store.append, MemoryRecord(content="x"))


def test_resolve_http_ref_is_state_store() -> None:
    store = resolve_memory("http://api:8000/agents/A/state/memory", {})
    assert isinstance(store, StateApiMemoryStore)


def test_resolve_unsupported_scheme_raises() -> None:
    with pytest.raises(MemoryError):
        resolve_memory("s3://bucket/mem", {})


def test_preamble_empty_is_none() -> None:
    assert format_memory_preamble([]) is None


def test_preamble_includes_content_and_traces() -> None:
    records = [
        MemoryRecord(
            content="deploy is a git push",
            provenance=Provenance(source_trace_ids=("t1",)),
        ),
        MemoryRecord(content="no-trace lesson"),
    ]
    preamble = format_memory_preamble(records)
    assert preamble is not None
    assert "deploy is a git push" in preamble
    assert "t1" in preamble
    assert "no-trace lesson" in preamble


def test_state_store_load_empty_is_empty() -> None:
    app, _ = _fake_state_app()

    async def go() -> None:
        async with TestServer(app) as server:
            url = str(server.make_url("/agents/A/state/memory"))
            store = StateApiMemoryStore(url, token=None)
            assert await store.load() == []

    anyio.run(go)


def test_state_store_append_then_load_round_trip() -> None:
    app, log = _fake_state_app()

    async def go() -> None:
        async with TestServer(app) as server:
            url = str(server.make_url("/agents/A/state/memory"))
            store = StateApiMemoryStore(url, token="k")
            rec = MemoryRecord(
                content="prod push reuses the dev bundle",
                provenance=Provenance(
                    learned_from_session_id="sess-9",
                    source_trace_ids=("trace-x",),
                    recorded_at="2026-07-13T00:00:00+00:00",
                ),
            )
            await store.append(rec)
            loaded = await store.load()
            assert loaded == [rec]
            # The provenance survived the persist/load round-trip.
            assert loaded[0].provenance.source_trace_ids == ("trace-x",)
            assert len(log) == 1

    anyio.run(go)


def test_session_runner_remember_appends_with_provenance() -> None:
    from agentos_runner import RunTracer, SideEffectClassifier
    from agentos_runner.fake import FakeModelSession
    from agentos_runner.session import SessionRunner

    class _Recording:
        def __init__(self) -> None:
            self.records: list[MemoryRecord] = []

        async def load(self) -> list[MemoryRecord]:
            return list(self.records)

        async def append(self, record: MemoryRecord) -> None:
            self.records.append(record)

    store = _Recording()
    runner = SessionRunner(
        session_factory=FakeModelSession,
        ceiling=0,
        tracer=RunTracer(None),
        classifier=SideEffectClassifier(),
        trace_name="t",
        session_id="sess-42",
        memory_store=store,
    )

    anyio.run(
        lambda: runner.remember("lesson", source_trace_ids=("trace-7",))
    )
    assert len(store.records) == 1
    prov = store.records[0].provenance
    assert store.records[0].content == "lesson"
    assert prov.learned_from_session_id == "sess-42"
    assert prov.source_trace_ids == ("trace-7",)
    assert prov.recorded_at  # a timestamp was stamped


def test_state_store_load_rejects_non_array() -> None:
    app = web.Application()

    async def get_log(_request: web.Request) -> web.Response:
        return web.json_response(
            {"namespace": "memory", "key": "log", "value": {"not": "a list"}, "version": 1}
        )

    app.router.add_get("/agents/A/state/memory/log", get_log)

    async def go() -> None:
        async with TestServer(app) as server:
            url = str(server.make_url("/agents/A/state/memory"))
            store = StateApiMemoryStore(url, token=None)
            with pytest.raises(MemoryError):
                await store.load()

    anyio.run(go)
