"""Harness for the eval-runner tests: a fake runner that answers eval_case turns
with a scripted per-input output, dialed by the real RunnerClient. Only the model
behind the runner is faked; the eval runner and grading are exercised for real.

The F3 eval-stream consumer tests additionally use a real MinIO bundle (uploaded
via ``bundles``) and, for the provisioned-runner path, the real G1 substrate wired
to a fake Kubernetes client whose sandbox resolves to the in-process fake runner.
Only the report HTTP POST is mocked (the external-service rule); Valkey, MinIO,
and Langfuse are always real."""

from __future__ import annotations

import contextlib
import io
import os
import uuid
import zipfile
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path

import pytest
from aci_protocol import Final, SessionStatus, ToolNote
from aci_protocol.s3 import build_s3_client
from agentos_worker.bundle_store import BundleStore
from agentos_worker.config import WorkerConfig
from agentos_worker.eval import EvalSuite
from agentos_worker.runner_client import RunnerClient
from aiohttp import web
from aiohttp.test_utils import TestServer

# The committed cross-language eval-case fixture, shared by the model and stream
# tests. conftest.py -> parents[2] is apps/worker.
EVAL_CASES_EXAMPLE_PATH = (
    Path(__file__).resolve().parents[2] / "schema" / "eval-cases.example.json"
)


@pytest.fixture
def eval_cases_example_path() -> Path:
    return EVAL_CASES_EXAMPLE_PATH


_VH = os.environ.get("TEST_VALKEY_HOST", "localhost")
_VP = int(os.environ.get("TEST_VALKEY_PORT", "26379"))
_VPW = os.environ.get("TEST_VALKEY_PW", "valkeypass")
_MINIO: dict[str, object] = {
    "s3_endpoint_url": os.environ.get("TEST_S3_ENDPOINT_URL", "http://localhost:29000"),
    "s3_access_key": os.environ.get("TEST_S3_ACCESS_KEY", "minio"),
    "s3_secret_key": os.environ.get("TEST_S3_SECRET_KEY", "miniosecret"),
    "s3_region": "us-east-1",
    "bundle_bucket": os.environ.get("TEST_BUNDLE_BUCKET", "agentos-bundles"),
}


class FakeEvalRunner:
    """Answers /v1/event with ``responses[input]`` (or 500 for ``fail_inputs``)."""

    def __init__(self) -> None:
        self.app = web.Application()
        self.app.add_routes(
            [web.post("/v1/event", self._event), web.get("/status", self._status)]
        )
        self.responses: dict[str, str] = {}
        self.fail_inputs: set[str] = set()
        # Inputs whose turn ends with a classified-failure final (budget/model
        # error) while still carrying text in responses[input].
        self.classified_failure_inputs: set[str] = set()
        # Inputs whose turn ends idle-awaiting-input (an incomplete turn) while
        # still carrying text in responses[input].
        self.idle_inputs: set[str] = set()
        # Per-input tool-call trajectory: the ordered tool names emitted as
        # tool_note frames before the final, so scorer-seam tests can drive the
        # tool-call sequence a turn produced.
        self.tool_calls: dict[str, list[str]] = {}
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
        if text in self.classified_failure_inputs:
            status = SessionStatus.CLASSIFIED_FAILURE
        elif text in self.idle_inputs:
            status = SessionStatus.IDLE_AWAITING_INPUT
        else:
            status = SessionStatus.DONE
        resp = web.StreamResponse(status=200, headers={"Content-Type": "application/x-ndjson"})
        await resp.prepare(request)
        for tool in self.tool_calls.get(text, []):
            note = ToolNote(text=f"calling {tool}", tool=tool)
            await resp.write((note.model_dump_json() + "\n").encode("utf-8"))
        frame = Final(text=output, status=status)
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


# --- Real MinIO bundle fixtures (the consumer loads suites from the bundle) ----


def bundle_zip(suite: EvalSuite) -> bytes:
    """A minimal plugin bundle: a zip carrying the suite at ``evals/cases.json``
    (the same layout the consumer's bundle loader reads)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("evals/cases.json", suite.model_dump_json())
    return buf.getvalue()


@pytest.fixture
def bundles() -> Iterator[tuple[BundleStore, Callable[[EvalSuite | bytes], str]]]:
    """A real read-only ``BundleStore`` plus an ``upload(suite_or_bytes) -> key``
    helper. Uploaded objects are deleted on teardown; skips if MinIO is down."""
    cfg = WorkerConfig(**_MINIO)  # type: ignore[arg-type]
    store = BundleStore(cfg)
    client = build_s3_client(
        endpoint_url=cfg.s3_endpoint_url,
        access_key=cfg.s3_access_key,
        secret_key=cfg.s3_secret_key,
        region=cfg.s3_region,
    )
    try:
        client.head_bucket(Bucket=cfg.bundle_bucket)
    except Exception as exc:  # noqa: BLE001 - any S3 failure means MinIO is unusable
        pytest.skip(f"MinIO bundle bucket not reachable: {exc}")
    keys: list[str] = []

    def upload(suite: EvalSuite | bytes) -> str:
        data = suite if isinstance(suite, bytes) else bundle_zip(suite)
        key = f"tests/bundles/{uuid.uuid4().hex}.zip"
        client.put_object(Bucket=cfg.bundle_bucket, Key=key, Body=data)
        keys.append(key)
        return key

    yield store, upload

    for key in keys:
        with contextlib.suppress(Exception):
            client.delete_object(Bucket=cfg.bundle_bucket, Key=key)
