"""Eval-job fan-out seam (K1): the API enqueues, a worker consumer runs the Job.

On a dev-branch push the git-flow deploy enqueues one eval job per new version
onto the Valkey stream ``agentos:evals``. Wire encoding mirrors the dispatcher's
``agentos:runs`` seam exactly: a single ``payload`` stream field holds the model
as ``model_dump_json()``, so fields can evolve without reshaping the stream.

This module is the PRODUCER only. The consumer (a worker-lane follow-up) reads
the stream and runs ``python -m agentos_worker.eval`` for the version; it should
reconstruct the request with ``EvalJobRequest.from_stream_fields``. See
docs/eval-fanout.md for the written contract.
"""

import uuid
from datetime import UTC, datetime
from typing import Any, cast

import redis.asyncio as redis
from pydantic import BaseModel

EVAL_STREAM = "agentos:evals"
STREAM_PAYLOAD_FIELD = "payload"


class EvalJobRequest(BaseModel):
    """One eval job: run ``suite`` against the version built from ``sha``.

    ``model`` is the model the worker should boot the eval sandbox with and tag
    the run's matrix cell by (#526); None keeps the worker default. Adding it is
    the intended forward-compatible evolution of the single-``payload`` seam -- an
    older consumer ignores the field, a newer one honours it.
    """

    agent_id: uuid.UUID
    version_id: uuid.UUID
    sha: str
    suite: str
    bundle_ref: str | None
    target_url: str | None = None
    model: str | None = None
    requested_at: str

    def to_stream_fields(self) -> dict[str, str]:
        return {STREAM_PAYLOAD_FIELD: self.model_dump_json()}

    @classmethod
    def from_stream_fields(cls, fields: dict[str, str]) -> "EvalJobRequest":
        return cls.model_validate_json(fields[STREAM_PAYLOAD_FIELD])


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class EvalQueue:
    def __init__(self, client: redis.Redis, stream: str = EVAL_STREAM) -> None:
        self._client = client
        self._stream = stream

    async def enqueue(self, request: EvalJobRequest) -> str:
        # redis-py types the fields map as an invariant broad union; cast to
        # satisfy the stub (same pattern as the dispatcher's enqueue).
        fields = cast(dict[Any, Any], request.to_stream_fields())
        stream_id = await self._client.xadd(self._stream, fields)
        return stream_id.decode() if isinstance(stream_id, bytes) else str(stream_id)
