"""Synthesize claude-agent-sdk messages from a live OpenCode `/event` stream.

This is the load-bearing shim for the OpenCode second harness (issue #25). The
runner core (``translate.py``, ``session.py``, ``otel.py``, the HTTP server, and
``packages/aci-protocol``) pattern-matches on claude-agent-sdk dataclasses
(``AssistantMessage`` / ``ResultMessage`` / ``TextBlock`` / ``ToolUseBlock``).
Rather than teach that core a second message vocabulary, this module re-emits
OpenCode's wire frames as those same dataclasses, so the whole runner runs
byte-for-byte unmodified against an OpenCode backend.

The mapping direction is:

    OpenCode /event frame  ->  claude-agent-sdk message  ->  (unchanged)
    translate.py  ->  ACI outbound event  ->  NDJSON

Unlike the offline spike (which collected a whole scripted turn then synthesized
it in one shot), a live turn arrives incrementally over Server-Sent Events, so
``TurnSynthesizer`` is a *stateful, incremental* mapper: feed it one frame at a
time and it returns the SDK messages to yield now, marking ``done`` at the turn
boundary. The relevant OpenCode v1 frames (verified against ``opencode`` v1.17.17
``GET /doc`` and a captured live turn) are:

- ``message.part.delta {field:"text", delta}`` -- one incremental text chunk
  (the new chunk only, not a growing snapshot). Each maps to one ``TextBlock``.
- ``message.part.updated {part}`` -- part snapshots. A ``tool`` part at
  ``state.status == "running"`` maps to one ``ToolUseBlock``; a ``step-finish``
  part carries token totals. The ``text`` part-updated snapshots (the empty
  text-start and full text-end bookends) are intentionally ignored so text
  already streamed via deltas is never emitted twice.
- ``message.updated {info}`` -- assistant message info carrying ``modelID`` and
  rolled-up ``tokens``.
- ``session.status {status:{type}}`` -- a ``busy`` -> ``idle`` transition marks
  turn completion (the terminal ``ResultMessage``). ``session.idle`` is the
  deprecated equivalent and is handled the same way.
- ``session.error {error}`` -- a hard failure, mapped to an error result.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock


def _tokens(tok: Any) -> dict[str, int] | None:
    """Fold an OpenCode token block into the SDK ``usage`` shape, or None."""

    if not isinstance(tok, dict):
        return None
    return {
        "input_tokens": int(tok.get("input", 0) or 0),
        "output_tokens": int(tok.get("output", 0) or 0),
    }


def _error_message(err: Any) -> str:
    """Extract a human string from an OpenCode ``session.error`` payload."""

    if isinstance(err, dict):
        data = err.get("data")
        if isinstance(data, dict) and data.get("message"):
            return str(data["message"])
        for key in ("message", "name"):
            if err.get(key):
                return str(err[key])
    return str(err) if err else "session error"


def _result(
    *, text: str, is_error: bool, usage: dict[str, int] | None
) -> ResultMessage:
    return ResultMessage(
        subtype="error" if is_error else "success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="opencode-session",
        result=text,
        usage=usage,
    )


class TurnSynthesizer:
    """Incrementally map one OpenCode turn's `/event` frames to SDK messages.

    Feed each frame to :meth:`ingest`; it returns zero or more SDK messages to
    yield immediately. When the turn terminates (``busy`` -> ``idle`` transition,
    a ``session.idle``, or a ``session.error``) it appends the single terminal
    ``ResultMessage`` and sets :attr:`done`. A turn only terminates on ``idle``
    once real turn activity has been seen (a ``busy`` status or streamed
    content), so a stale pre-turn ``idle`` on the shared bus cannot end a turn
    early.
    """

    def __init__(self) -> None:
        self.done = False
        self._final_text = ""
        self._usage: dict[str, int] | None = None
        self._model_id: str | None = None
        self._error: str | None = None
        self._seen_activity = False

    def _terminal(self) -> ResultMessage:
        if self._error is not None:
            return _result(text=self._error, is_error=True, usage=self._usage)
        return _result(text=self._final_text, is_error=False, usage=self._usage)

    def ingest(self, frame: dict[str, Any]) -> list[Any]:
        if self.done:
            return []
        ftype = frame.get("type")
        props = frame.get("properties", {})
        if not isinstance(props, dict):
            return []
        out: list[Any] = []

        if ftype == "message.part.delta" and props.get("field") == "text":
            chunk = props.get("delta", "")
            if chunk:
                self._seen_activity = True
                self._final_text += chunk
                out.append(
                    AssistantMessage(content=[TextBlock(text=chunk)], model=self._model_id or "")
                )

        elif ftype == "message.part.updated":
            part = props.get("part", {})
            if isinstance(part, dict):
                ptype = part.get("type")
                if ptype in ("text", "step-start", "step-finish", "reasoning"):
                    self._seen_activity = True
                if ptype == "tool":
                    state = part.get("state", {}) if isinstance(part.get("state"), dict) else {}
                    status = state.get("status")
                    if status == "running":
                        self._seen_activity = True
                        out.append(
                            AssistantMessage(
                                content=[
                                    ToolUseBlock(
                                        id=str(part.get("callID") or part.get("id") or "call"),
                                        name=str(part.get("tool") or "unknown"),
                                        input=state.get("input") or {},
                                    )
                                ],
                                model=self._model_id or "",
                            )
                        )
                    elif status == "error":
                        self._error = str(state.get("error") or "tool failed")
                elif ptype == "step-finish":
                    u = _tokens(part.get("tokens"))
                    if u is not None:
                        self._usage = u

        elif ftype == "message.updated":
            info = props.get("info", {})
            if isinstance(info, dict):
                model = info.get("modelID")
                if model:
                    self._model_id = str(model)
                u = _tokens(info.get("tokens"))
                if u is not None:
                    self._usage = u

        elif ftype == "session.error":
            self._error = _error_message(props.get("error"))
            out.append(self._terminal())
            self.done = True

        elif ftype in ("session.status", "session.idle"):
            status = props.get("status") if ftype == "session.status" else {"type": "idle"}
            stype = status.get("type") if isinstance(status, dict) else None
            if stype == "busy":
                self._seen_activity = True
            elif stype == "idle" and self._seen_activity:
                out.append(self._terminal())
                self.done = True

        return out
