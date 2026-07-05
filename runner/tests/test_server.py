"""The aiohttp ACI channel: health, status, event stream, interrupt, steer."""

import anyio
from aci_protocol import SessionStatus, parse_ndjson
from agentos_runner import RunTracer, SideEffectClassifier, create_app
from agentos_runner.fake import FakeModelSession
from agentos_runner.session import SessionRunner
from aiohttp.test_utils import TestClient, TestServer


def _runner() -> tuple[SessionRunner, FakeModelSession]:
    fake = FakeModelSession()
    runner = SessionRunner(
        session_factory=lambda: fake,
        ceiling=0,
        tracer=RunTracer(None),
        classifier=SideEffectClassifier(),
        trace_name="t",
    )
    return runner, fake


def test_healthz_status_and_event_round_trip() -> None:
    runner, _ = _runner()

    async def go() -> None:
        await runner.start()
        async with TestClient(TestServer(create_app(runner))) as client:
            health = await client.get("/healthz")
            assert health.status == 200
            assert (await health.json())["ok"] is True

            status = await client.get("/status")
            body = await status.json()
            assert body["status"] == SessionStatus.IDLE_AWAITING_INPUT.value
            assert body["ready"] is True

            frame = {"kind": "event", "type": "message", "text": "hi", "user": "U", "ts": "1"}
            resp = await client.post("/v1/event", json=frame)
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("application/x-ndjson")
            events = parse_ndjson(await resp.text())
            assert events[-1].type == "final"
            assert events[-1].status == SessionStatus.DONE

    anyio.run(go)


def test_event_rejects_non_event_frame() -> None:
    runner, _ = _runner()

    async def go() -> None:
        await runner.start()
        async with TestClient(TestServer(create_app(runner))) as client:
            resp = await client.post("/v1/event", json={"kind": "interrupt", "reason": "x"})
            assert resp.status == 400

    anyio.run(go)


def test_interrupt_endpoint_acks() -> None:
    runner, fake = _runner()

    async def go() -> None:
        await runner.start()
        async with TestClient(TestServer(create_app(runner))) as client:
            resp = await client.post("/v1/interrupt", json={"kind": "interrupt", "reason": "stop"})
            assert resp.status == 200
            assert (await resp.json())["ok"] is True
            assert fake.interrupts >= 1

    anyio.run(go)


def test_steer_takes_an_event_frame_and_conflicts_without_a_turn() -> None:
    runner, _ = _runner()
    steer_frame = {"kind": "event", "type": "message", "text": "do X", "user": "U", "ts": "2"}

    async def go() -> None:
        await runner.start()
        async with TestClient(TestServer(create_app(runner))) as client:
            # A steer is an ACI event frame; with no live turn it has nowhere to
            # land -> 409, so F1 falls back to opening a fresh /v1/event.
            resp = await client.post("/v1/steer", json=steer_frame)
            assert resp.status == 409
            # A non-event frame on the steer endpoint is a 400.
            bad = await client.post("/v1/steer", json={"kind": "interrupt", "reason": "x"})
            assert bad.status == 400

    anyio.run(go)
