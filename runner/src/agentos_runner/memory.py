"""The memory port: resolve ``AGENTOS_MEMORY_REF`` and load/append agent memory.

This is the first loader for the memory seam (issue #264, epic #28). Until now
``memory_ref`` was a ``SessionConfig`` field carried end-to-end but never
dereferenced (see ``docs/interfaces/memory/INTERFACE.md``). This module defines
the port and its first concrete backing.

Design (ADR-0025):

- **Memory lives outside the sandbox** (ADR-0003, stateless-first). A resumed
  thread must rehydrate from an external, durable resource, never from surviving
  in-process state. So the store is reached over the network at boot, not held in
  pod-local scratch.
- **The backing reuses the durable KV/document store** landed for #23/#248
  (``apps/api`` ``/agents/{agent_id}/state/{namespace}/{key}``, Postgres JSONB),
  rather than inventing a new datastore. Memory is a **scoped namespace** over
  that store: the log-shaped ``append`` endpoint gives us the append-only,
  provenance-carrying write the memory port needs, and ``get`` gives us load.
- **The port is small and swappable** -- ``load`` / ``append`` -- matching the
  "seventh swappable job, one default backing" framing of the interface doc. A
  future S3- or API-backed loader is a drop-in ``MemoryStore``.

``AGENTOS_MEMORY_REF`` resolution: the ref is the URL of the agent's memory
namespace on the state API (e.g. ``http://api:8000/agents/<id>/state/memory``).
The runner authenticates to that API with ``AGENTOS_MEMORY_TOKEN`` (a
runner-local knob, like ``AGENTOS_RUNNER_TOKEN``/``AGENTOS_MODEL`` -- NOT part of
the frozen ACI ``SessionConfig`` env, so no frozen-contract change). An ``s3://``
or other scheme is reserved for a future loader and rejected loudly today.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import aiohttp

logger = logging.getLogger(__name__)

# The single log-shaped key inside the agent's memory namespace. The whole
# namespace is reserved for memory; one append-only log key keeps load a single
# GET and append a single POST against the #248 log endpoint.
MEMORY_LOG_KEY = "log"

# Runner-local env carrying the bearer the state API expects (X-API-Key). Not a
# model credential and not part of the frozen ACI SessionConfig -- resolved the
# same way as the other runner-local knobs in config.py.
MEMORY_TOKEN_ENV = "AGENTOS_MEMORY_TOKEN"


class MemoryError(RuntimeError):
    """A memory reference could not be resolved or dereferenced."""


@dataclass(frozen=True)
class Provenance:
    """Where a memory record was learned from -- links the entry to its sources.

    ``learned_from_session_id`` is the ACI session that produced the record;
    ``source_trace_ids`` are the OTel/Langfuse trace ids of the turns the lesson
    was distilled from. ``recorded_at`` is set at append time. This is the shape
    the epic (#28) calls for: entry -> source trace ids.
    """

    learned_from_session_id: str | None = None
    source_trace_ids: tuple[str, ...] = ()
    recorded_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "learned_from_session_id": self.learned_from_session_id,
            "source_trace_ids": list(self.source_trace_ids),
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Provenance:
        return cls(
            learned_from_session_id=data.get("learned_from_session_id"),
            source_trace_ids=tuple(data.get("source_trace_ids") or ()),
            recorded_at=data.get("recorded_at", ""),
        )


@dataclass(frozen=True)
class MemoryRecord:
    """One durable memory entry: the learned content plus its provenance."""

    content: str
    provenance: Provenance = field(default_factory=Provenance)

    def to_dict(self) -> dict[str, Any]:
        return {"content": self.content, "provenance": self.provenance.to_dict()}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MemoryRecord:
        prov = data.get("provenance")
        return cls(
            content=data["content"],
            provenance=Provenance.from_dict(prov) if isinstance(prov, Mapping) else Provenance(),
        )


@runtime_checkable
class MemoryStore(Protocol):
    """The memory port: load prior records, append a new one with provenance.

    Deliberately narrow -- no query language, no consolidate (that is a later
    slice, #265/#266/#267). A concrete store dereferences a ``memory_ref`` to a
    durable, rehydratable backing that lives outside the sandbox.
    """

    async def load(self) -> list[MemoryRecord]:
        """Return prior memory records, oldest first (empty when none)."""
        ...

    async def append(self, record: MemoryRecord) -> None:
        """Durably append one record; it must survive suspend/resume."""
        ...


class NullMemoryStore:
    """The no-memory store used when ``AGENTOS_MEMORY_REF`` is unset.

    ``load`` yields nothing and ``append`` is a silent no-op, so the boot path is
    uniform whether or not an agent has memory configured.
    """

    async def load(self) -> list[MemoryRecord]:
        return []

    async def append(self, record: MemoryRecord) -> None:  # noqa: ARG002 - null sink
        return None


class StateApiMemoryStore:
    """Memory backed by the durable state store (#23/#248), the default loader.

    ``memory_ref`` is the URL of the agent's memory namespace on the state API
    (``.../agents/<id>/state/memory``). Load is a GET of the single log key;
    append is a POST to the log's ``/append`` endpoint. The state API enforces
    the size caps (#248) and the Postgres JSONB backing gives durability across
    suspend/resume for free.
    """

    def __init__(self, namespace_url: str, token: str | None) -> None:
        # Normalize to no trailing slash so key URLs compose cleanly.
        self._base = namespace_url.rstrip("/")
        self._token = token

    @property
    def _log_url(self) -> str:
        return f"{self._base}/{MEMORY_LOG_KEY}"

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self._token} if self._token else {}

    async def load(self) -> list[MemoryRecord]:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(self._log_url, headers=self._headers()) as resp:
                if resp.status == 404:
                    # No memory written yet -- a fresh agent, not an error.
                    return []
                if resp.status != 200:
                    body = await resp.text()
                    raise MemoryError(
                        f"memory load failed: {resp.status} {body[:200]}"
                    )
                payload = await resp.json()
        value = payload.get("value")
        if not isinstance(value, list):
            raise MemoryError("memory log is not a JSON array")
        records: list[MemoryRecord] = []
        for item in value:
            if isinstance(item, Mapping) and "content" in item:
                records.append(MemoryRecord.from_dict(item))
        return records

    async def append(self, record: MemoryRecord) -> None:
        timeout = aiohttp.ClientTimeout(total=15)
        body = json.dumps({"item": record.to_dict()})
        headers = {**self._headers(), "Content-Type": "application/json"}
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{self._log_url}/append", data=body, headers=headers
            ) as resp:
                if resp.status not in (200, 201):
                    text = await resp.text()
                    raise MemoryError(
                        f"memory append failed: {resp.status} {text[:200]}"
                    )


def resolve_memory(memory_ref: str | None, env: Mapping[str, str]) -> MemoryStore:
    """Resolve ``AGENTOS_MEMORY_REF`` to a concrete ``MemoryStore`` at boot.

    An absent ref yields the ``NullMemoryStore`` (memory is optional). An
    ``http(s)://`` ref is the state-API namespace URL and yields the default
    ``StateApiMemoryStore``. Any other scheme (``s3://`` etc.) is reserved for a
    future loader and rejected loudly rather than silently ignored, so a
    misconfigured ref fails visibly at boot rather than dropping memory.
    """

    if not memory_ref:
        return NullMemoryStore()
    if memory_ref.startswith(("http://", "https://")):
        return StateApiMemoryStore(memory_ref, env.get(MEMORY_TOKEN_ENV))
    raise MemoryError(
        f"unsupported AGENTOS_MEMORY_REF scheme: {memory_ref!r} "
        "(only http(s):// state-API refs are implemented today)"
    )


def format_memory_preamble(records: Sequence[MemoryRecord]) -> str | None:
    """Render prior memory as a system-prompt preamble, or None when empty.

    This is how loaded memory is *delivered into the sandbox*: it is composed
    into the runner's effective system prompt at boot, so the model sees prior
    lessons as durable context. Provenance is summarized inline so a lesson is
    traceable back to the turns it came from.
    """

    if not records:
        return None
    lines = ["# Agent memory (learned from prior sessions)", ""]
    for record in records:
        prov = record.provenance
        traces = ", ".join(prov.source_trace_ids) if prov.source_trace_ids else ""
        suffix = f"  (learned from traces: {traces})" if traces else ""
        lines.append(f"- {record.content}{suffix}")
    return "\n".join(lines)


def utcnow_iso() -> str:
    """An RFC3339 UTC timestamp for a provenance record's ``recorded_at``."""
    return datetime.now(UTC).isoformat()
