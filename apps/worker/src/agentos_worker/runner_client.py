"""Async HTTP client for the runner's ACI channel.

The runner (D1) exposes the ACI session over HTTP: ``POST /v1/event`` opens a turn
and streams outbound NDJSON to a ``final``; ``POST /v1/steer`` injects a follow-up
into the live turn (409 when no turn is active, the finish-race boundary the
kernel owns); ``POST /v1/interrupt`` hard-stops; ``GET /status`` reports turn
state. This client turns those into typed calls the kernel composes.

The turn is split into ``start_turn`` (awaits the response headers, at which point
the runner's turn is active) and iterating the returned ``TurnStream`` (the
NDJSON body). That split lets the kernel establish the active turn while holding
the per-thread lock, then release the lock and stream the body, so a concurrent
follow-up can only steer the live turn and never fork a second one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import TracebackType

import aiohttp
from aci_protocol import Event, Interrupt, OutboundEvent, parse_ndjson_line


def _auth_headers(token: str | None) -> dict[str, str] | None:
    """Per-call Authorization header for the per-sandbox runner token (issue #63).

    The ClientSession is worker-wide and dials many base_urls, so the token is a
    per-call header, never a session default -- a default would leak one sandbox's
    token to every other. Returns None (no header) when the token is unset/empty.
    """
    if token:
        return {"Authorization": f"Bearer {token}"}
    return None


class RunnerError(Exception):
    """The runner returned an unexpected HTTP status or an unreadable stream."""


class TurnStream:
    """An open ``/v1/event`` response: the turn is active; iterate for frames."""

    def __init__(self, response: aiohttp.ClientResponse) -> None:
        self._response = response

    async def __aiter__(self) -> AsyncIterator[OutboundEvent]:
        async for raw in self._response.content:
            line = raw.decode("utf-8").strip()
            if not line:
                continue
            yield parse_ndjson_line(line)

    def close(self) -> None:
        self._response.release()

    async def __aenter__(self) -> TurnStream:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class RunnerClient:
    """Dials a claimed runner over its base_url. One client serves all threads."""

    def __init__(
        self,
        *,
        connect_timeout_s: float = 10.0,
        total_timeout_s: float = 600.0,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._own_session = session is None
        self._session = session or aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(
                total=total_timeout_s, connect=connect_timeout_s, sock_read=total_timeout_s
            )
        )

    async def start_turn(
        self, base_url: str, event: Event, token: str | None = None
    ) -> TurnStream:
        """Open a turn. Returns once the runner has accepted it (turn active)."""
        resp = await self._session.post(
            f"{base_url}/v1/event", json=event.model_dump(), headers=_auth_headers(token)
        )
        if resp.status != 200:
            body = await resp.text()
            resp.release()
            raise RunnerError(f"/v1/event -> {resp.status}: {body}")
        return TurnStream(resp)

    async def steer(self, base_url: str, event: Event, token: str | None = None) -> bool:
        """Inject a follow-up into the live turn. False on 409 (no active turn)."""
        async with self._session.post(
            f"{base_url}/v1/steer", json=event.model_dump(), headers=_auth_headers(token)
        ) as resp:
            if resp.status == 409:
                return False
            if resp.status != 200:
                body = await resp.text()
                raise RunnerError(f"/v1/steer -> {resp.status}: {body}")
            return True

    async def interrupt(self, base_url: str, reason: str, token: str | None = None) -> None:
        """Hard-stop the live turn; its final is reclassified to idle."""
        frame = Interrupt(reason=reason)
        async with self._session.post(
            f"{base_url}/v1/interrupt", json=frame.model_dump(), headers=_auth_headers(token)
        ) as resp:
            if resp.status not in (200, 409):
                body = await resp.text()
                raise RunnerError(f"/v1/interrupt -> {resp.status}: {body}")

    async def reset(self, base_url: str, token: str | None = None) -> None:
        """Discard the runner's conversation so the next turn starts fresh (#550).

        The eval driver calls this between cases to enforce per-case isolation.
        A 409 (a turn is still active) is surfaced as a ``RunnerError`` like any
        other unexpected status: the eval flow is sequential, so a turn should
        never be live at reset time -- a 409 here is a real ordering bug, not a
        condition to swallow.
        """
        async with self._session.post(
            f"{base_url}/v1/reset", headers=_auth_headers(token)
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RunnerError(f"/v1/reset -> {resp.status}: {body}")

    async def status(self, base_url: str) -> dict[str, object]:
        async with self._session.get(f"{base_url}/status") as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RunnerError(f"/status -> {resp.status}: {body}")
            data: dict[str, object] = await resp.json()
            return data

    async def close(self) -> None:
        if self._own_session:
            await self._session.close()

    async def __aenter__(self) -> RunnerClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()
