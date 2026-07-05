"""aiohttp server exposing the ACI channel over HTTP.

Productizes the prototype's aiohttp ``/run`` into the ACI session channel:

- ``GET  /healthz``      liveness (always ok once the process is up)
- ``GET  /status``       session status: done / idle-awaiting-input /
                         classified-failure, plus readiness and turn state
- ``POST /v1/event``     open a turn: body is an ACI ``event`` frame; the
                         response streams outbound NDJSON, ending in a final
- ``POST /v1/steer``     inject a follow-up ACI ``event`` frame into the live
                         turn (same frame type as ``/v1/event``); 409 when no turn
                         is active (the finish-race boundary F1 owns), so the
                         caller falls back to a fresh ``/v1/event``
- ``POST /v1/interrupt`` hard-stop the live turn: body is an ACI ``interrupt``
                         frame; the open turn's final is reclassified to idle

One turn consumes the SDK generator at a time (enforced by the runner's turn
lock); steer and interrupt are side-channel injections whose output surfaces on
the open ``/v1/event`` stream, exactly as the PT-2 steering proof showed.
"""

from __future__ import annotations

from typing import cast

from aci_protocol import Event, Interrupt, parse_inbound
from aiohttp import web

from .session import SessionRunner

_NDJSON = "application/x-ndjson"

# Typed app key so aiohttp resolves the runner without the string-key warning.
RUNNER: web.AppKey[SessionRunner] = web.AppKey("runner", SessionRunner)


def create_app(runner: SessionRunner) -> web.Application:
    """Build the aiohttp application bound to a started SessionRunner."""

    app = web.Application()
    app[RUNNER] = runner
    app.add_routes(
        [
            web.get("/healthz", _healthz),
            web.get("/status", _status),
            web.post("/v1/event", _event),
            web.post("/v1/steer", _steer),
            web.post("/v1/interrupt", _interrupt),
        ]
    )
    app.on_cleanup.append(_on_cleanup)
    return app


async def _on_cleanup(app: web.Application) -> None:
    await app[RUNNER].close()


async def _healthz(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def _status(request: web.Request) -> web.Response:
    runner: SessionRunner = request.app[RUNNER]
    return web.json_response(
        {
            "status": runner.status.value,
            "ready": runner.ready,
            "turn_active": runner.turn_active,
        }
    )


def _parse(body: object) -> Event | Interrupt:
    # parse_inbound validates against the frozen InboundMessage union; the
    # runtime type is always Event | Interrupt though the signature is Any.
    return cast("Event | Interrupt", parse_inbound(cast("dict[str, object]", body)))


async def _event(request: web.Request) -> web.StreamResponse:
    runner: SessionRunner = request.app[RUNNER]
    try:
        frame = _parse(await request.json())
    except Exception as exc:  # noqa: BLE001 - map any decode/validation error to 400
        return web.json_response({"error": f"invalid event frame: {exc}"}, status=400)
    if not isinstance(frame, Event):
        return web.json_response(
            {"error": "expected an event frame; use /v1/interrupt for interrupts"},
            status=400,
        )

    response = web.StreamResponse(status=200, headers={"Content-Type": _NDJSON})
    await response.prepare(request)
    async for line in runner.run_turn(frame):
        await response.write(line.encode("utf-8"))
    await response.write_eof()
    return response


async def _steer(request: web.Request) -> web.Response:
    runner: SessionRunner = request.app[RUNNER]
    try:
        frame = _parse(await request.json())
    except Exception as exc:  # noqa: BLE001
        return web.json_response({"error": f"invalid steer frame: {exc}"}, status=400)
    if not isinstance(frame, Event):
        return web.json_response({"error": "expected an event frame"}, status=400)

    delivered = await runner.steer(frame.text)
    if not delivered:
        return web.json_response(
            {"error": "no active turn to steer; open a new /v1/event"}, status=409
        )
    return web.json_response({"ok": True})


async def _interrupt(request: web.Request) -> web.Response:
    runner: SessionRunner = request.app[RUNNER]
    try:
        frame = _parse(await request.json())
    except Exception as exc:  # noqa: BLE001
        return web.json_response({"error": f"invalid interrupt frame: {exc}"}, status=400)
    if not isinstance(frame, Interrupt):
        return web.json_response({"error": "expected an interrupt frame"}, status=400)

    await runner.interrupt(frame.reason)
    return web.json_response({"ok": True})
