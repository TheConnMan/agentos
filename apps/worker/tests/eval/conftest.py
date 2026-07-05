"""Harness for the eval-runner tests: a fake runner that answers eval_case turns
with a scripted per-input output, dialed by the real RunnerClient. Only the model
behind the runner is faked; the eval runner and grading are exercised for real."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable

import pytest
from aci_protocol import Final, SessionStatus
from agentos_worker.runner_client import RunnerClient
from aiohttp import web
from aiohttp.test_utils import TestServer


class FakeEvalRunner:
    """Answers /v1/event with ``responses[input]`` (or 500 for ``fail_inputs``)."""

    def __init__(self) -> None:
        self.app = web.Application()
        self.app.add_routes(
            [web.post("/v1/event", self._event), web.get("/status", self._status)]
        )
        self.responses: dict[str, str] = {}
        self.fail_inputs: set[str] = set()
        self.default_output = ""
        self.seen: list[dict[str, str]] = []

    async def _status(self, _request: web.Request) -> web.Response:
        return web.json_response({"status": "done", "turn_active": False})

    async def _event(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        self.seen.append(body)
        text = body["text"]
        if text in self.fail_inputs:
            return web.json_response({"error": "boom"}, status=500)
        output = self.responses.get(text, self.default_output)
        resp = web.StreamResponse(status=200, headers={"Content-Type": "application/x-ndjson"})
        await resp.prepare(request)
        frame = Final(text=output, status=SessionStatus.DONE)
        await resp.write((frame.model_dump_json() + "\n").encode("utf-8"))
        await resp.write_eof()
        return resp


@contextlib.asynccontextmanager
async def _eval_harness() -> AsyncIterator[tuple[str, FakeEvalRunner, RunnerClient]]:
    fake = FakeEvalRunner()
    server = TestServer(fake.app)
    await server.start_server()
    base_url = f"http://127.0.0.1:{server.port}"
    client = RunnerClient(total_timeout_s=30.0)
    try:
        yield base_url, fake, client
    finally:
        with contextlib.suppress(Exception):
            await client.close()
        with contextlib.suppress(Exception):
            await server.close()


@pytest.fixture
def make_eval_harness() -> Callable[
    [], contextlib.AbstractAsyncContextManager[tuple[str, FakeEvalRunner, RunnerClient]]
]:
    def factory() -> (
        contextlib.AbstractAsyncContextManager[tuple[str, FakeEvalRunner, RunnerClient]]
    ):
        return _eval_harness()

    return factory
