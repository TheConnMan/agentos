"""The conversation-history port: resolve ``AGENTOS_HISTORY_REF`` and load/append
this thread's transcript.

This is the first loader for the history seam (issue #20). Until now
``AGENTOS_HISTORY_REF`` was a runner-local ref read into ``RunnerConfig`` and fed
to the SDK ``resume=`` option, but nothing produced a usable value and the SDK
transcript it pointed at lived on the pod's emptyDir (lost on any restart). This
module is the sibling of ``memory.py`` (ADR-0025): the same durable state store,
the same boot-preamble delivery, a different scope.

Design (ADR-0029):

- **History lives outside the sandbox** (ADR-0003, stateless-first). An unplanned
  runner-pod restart is a new pod with empty scratch, so a restarted thread must
  rehydrate from an external, durable resource reached over the network at boot.
- **The backing reuses the durable state store** landed for #23/#248 and #264
  (``apps/api`` ``/agents/{agent_id}/state/{namespace}/{key}``, Postgres JSONB),
  rather than inventing a new datastore. The thread's transcript is the
  log-shaped key ``.../state/transcript/<thread_key>``: the ``append`` endpoint
  gives the append-only write and ``get`` gives load.
- **Harness-agnostic delivery.** Loaded turns are composed into the effective
  system prompt as a conversation preamble, not replayed through any one
  harness's resume API, so a second harness (ADR-0011/0021) rehydrates the same
  way.

``AGENTOS_HISTORY_REF`` resolution: the ref is the URL of the thread's transcript
key on the state API (e.g. ``http://api:8000/agents/<id>/state/transcript/<thread>``).
The runner authenticates with ``AGENTOS_HISTORY_TOKEN`` (a runner-local knob, like
``AGENTOS_MEMORY_TOKEN`` -- NOT part of the frozen ACI ``SessionConfig``, so no
frozen-contract change). An ``s3://`` or other scheme is reserved for a future
loader and rejected loudly today.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import aiohttp

logger = logging.getLogger(__name__)

# Runner-local env carrying the bearer the state API expects (X-API-Key). Not a
# model credential and not part of the frozen ACI SessionConfig -- resolved the
# same way as AGENTOS_MEMORY_TOKEN and the other runner-local knobs.
HISTORY_TOKEN_ENV = "AGENTOS_HISTORY_TOKEN"


class HistoryError(RuntimeError):
    """A history reference could not be resolved or dereferenced."""


@dataclass(frozen=True)
class TurnRecord:
    """One conversation turn: the user message and the assistant's reply.

    ``ts`` is set at append time (RFC3339 UTC) so a reloaded transcript keeps its
    order and a turn is timestamped for debugging. The pair is the minimal
    harness-agnostic unit: any harness can render it as prior context.
    """

    user: str
    assistant: str
    ts: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"user": self.user, "assistant": self.assistant, "ts": self.ts}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TurnRecord:
        return cls(
            user=data.get("user", ""),
            assistant=data.get("assistant", ""),
            ts=data.get("ts", ""),
        )


@runtime_checkable
class TranscriptStore(Protocol):
    """The history port: load prior turns, append the latest one.

    Deliberately narrow -- no query language, no summarization (windowing is a
    later slice). A concrete store dereferences a ``history_ref`` to a durable,
    rehydratable backing that lives outside the sandbox.
    """

    async def load(self) -> list[TurnRecord]:
        """Return prior turns, oldest first (empty when none)."""
        ...

    async def append(self, record: TurnRecord) -> None:
        """Durably append one turn; it must survive an unplanned restart."""
        ...


class NullTranscriptStore:
    """The no-history store used when ``AGENTOS_HISTORY_REF`` is unset.

    ``load`` yields nothing and ``append`` is a silent no-op, so the boot and
    per-turn paths are uniform whether or not a thread has a transcript ref.
    """

    async def load(self) -> list[TurnRecord]:
        return []

    async def append(self, record: TurnRecord) -> None:  # noqa: ARG002 - null sink
        return None


class StateApiTranscriptStore:
    """Transcript backed by the durable state store (#23/#248/#264), the default.

    ``history_ref`` is the URL of the thread's transcript key on the state API
    (``.../agents/<id>/state/transcript/<thread_key>``). Load is a GET of that
    key; append is a POST to the key's ``/append`` endpoint. The state API
    enforces the size caps and the Postgres JSONB backing gives durability across
    an unplanned restart for free.
    """

    def __init__(self, key_url: str, token: str | None) -> None:
        # Normalize to no trailing slash so the /append URL composes cleanly.
        self._key_url = key_url.rstrip("/")
        self._token = token

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self._token} if self._token else {}

    async def load(self) -> list[TurnRecord]:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(self._key_url, headers=self._headers()) as resp:
                if resp.status == 404:
                    # No transcript written yet -- a fresh thread, not an error.
                    return []
                if resp.status != 200:
                    body = await resp.text()
                    raise HistoryError(f"history load failed: {resp.status} {body[:200]}")
                payload = await resp.json()
        value = payload.get("value")
        if not isinstance(value, list):
            raise HistoryError("transcript log is not a JSON array")
        turns: list[TurnRecord] = []
        for item in value:
            if isinstance(item, Mapping) and "user" in item:
                turns.append(TurnRecord.from_dict(item))
        return turns

    async def append(self, record: TurnRecord) -> None:
        timeout = aiohttp.ClientTimeout(total=15)
        body = json.dumps({"item": record.to_dict()})
        headers = {**self._headers(), "Content-Type": "application/json"}
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{self._key_url}/append", data=body, headers=headers
            ) as resp:
                if resp.status not in (200, 201):
                    text = await resp.text()
                    raise HistoryError(f"history append failed: {resp.status} {text[:200]}")


def resolve_history(history_ref: str | None, env: Mapping[str, str]) -> TranscriptStore:
    """Resolve ``AGENTOS_HISTORY_REF`` to a concrete ``TranscriptStore`` at boot.

    An absent ref yields the ``NullTranscriptStore`` (history is optional). An
    ``http(s)://`` ref is the state-API transcript-key URL and yields the default
    ``StateApiTranscriptStore``. Any other scheme (an old SDK ``resume`` id,
    ``s3://``, ...) is reserved for a future loader and rejected loudly rather
    than silently dropped, so a misconfigured ref fails visibly at boot.
    """

    if not history_ref:
        return NullTranscriptStore()
    if history_ref.startswith(("http://", "https://")):
        return StateApiTranscriptStore(history_ref, env.get(HISTORY_TOKEN_ENV))
    raise HistoryError(
        f"unsupported AGENTOS_HISTORY_REF scheme: {history_ref!r} "
        "(only http(s):// state-API refs are implemented today)"
    )


# Sane preamble caps. A restarted thread should see recent context without the
# system prompt ballooning: ~40 turns keeps a long conversation's continuity, and
# ~16 KB bounds the rendered history to a few KB of the boot prompt. Both are
# overridable at the boot call site (AGENTOS_HISTORY_MAX_TURNS/_BYTES) and either
# can be disabled with None.
DEFAULT_PREAMBLE_MAX_TURNS = 40
DEFAULT_PREAMBLE_MAX_BYTES = 16_000

# Prepended when older turns were dropped, so the model knows its recovered
# context is a tail window, not the whole thread. Must contain "elided".
_ELISION_NOTE = "(earlier turns elided to fit the context budget)"


def _render_preamble(records: Sequence[TurnRecord], *, elided: bool) -> str:
    """Render the given turns to the preamble text (with an optional elision note).

    With ``elided=False`` this is byte-identical to the original unbounded render,
    so an under-cap transcript is unchanged.
    """

    lines = [
        "# Conversation so far (recovered after a restart)",
        "",
        "This thread continued from earlier turns. Prior exchange, oldest first:",
        "",
    ]
    if elided:
        lines.append(_ELISION_NOTE)
        lines.append("")
    for record in records:
        lines.append(f"User: {record.user}")
        lines.append(f"Assistant: {record.assistant}")
        lines.append("")
    return "\n".join(lines).rstrip()


def format_conversation_preamble(
    records: Sequence[TurnRecord],
    *,
    max_turns: int | None = DEFAULT_PREAMBLE_MAX_TURNS,
    max_bytes: int | None = DEFAULT_PREAMBLE_MAX_BYTES,
) -> str | None:
    """Render prior turns as a bounded system-prompt preamble, or None when empty.

    This is how a reloaded transcript is delivered into the sandbox: it is
    composed into the runner's effective system prompt at boot, so a restarted
    session sees the prior exchange as context (harness-agnostic -- it is plain
    prompt text, not a resume through any one harness's API).

    The preamble is windowed to the most-recent (tail) turns: when the turn count
    exceeds ``max_turns`` or the rendered size exceeds ``max_bytes``, the oldest
    turns are dropped until it fits and an elision note is prepended. Either cap is
    disabled by passing None; with both None the render is byte-identical to the
    unbounded output and carries no note (delivery-side windowing only -- the
    stored transcript is untouched).
    """

    if not records:
        return None
    total = len(records)
    kept = list(records)
    # Cap by turn count: keep the most-recent ``max_turns`` turns.
    if max_turns is not None and len(kept) > max_turns:
        kept = kept[-max_turns:]
    # Cap by rendered size: drop oldest turns until the render fits the byte
    # budget, always keeping at least the single most-recent turn. Render with the
    # elision flag it will actually carry so the note's own bytes are accounted for,
    # and only re-render when ``kept`` actually shrinks (no duplicate final render).
    rendered = _render_preamble(kept, elided=len(kept) < total)
    if max_bytes is not None:
        while len(kept) > 1 and len(rendered.encode("utf-8")) > max_bytes:
            kept = kept[1:]
            rendered = _render_preamble(kept, elided=len(kept) < total)
    return rendered


def utcnow_iso() -> str:
    """An RFC3339 UTC timestamp for a turn record's ``ts``."""
    return datetime.now(UTC).isoformat()
