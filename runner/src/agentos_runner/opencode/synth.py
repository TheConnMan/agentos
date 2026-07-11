"""Synthesize runner TurnEvents from a live OpenCode `/event` stream.

This is the load-bearing shim for the OpenCode second harness (issue #25). The
runner core (``translate.py``, ``session.py``, ``otel.py``, the HTTP server, and
``packages/aci-protocol``) pattern-matches on the runner-owned ``TurnEvent`` union
(``AssistantText`` / ``ToolCall`` / ``TurnResult``). This module re-emits
OpenCode's wire frames as those TurnEvents, so the whole runner runs unmodified
against an OpenCode backend.

The mapping direction is:

    OpenCode /event frame  ->  runner TurnEvent  ->  (unchanged)
    translate.py  ->  ACI outbound event  ->  NDJSON

Unlike the offline spike (which collected a whole scripted turn then synthesized
it in one shot), a live turn arrives incrementally over Server-Sent Events, so
``TurnSynthesizer`` is a *stateful, incremental* mapper: feed it one frame at a
time and it returns the TurnEvents to yield now, marking ``done`` at the turn
boundary. The relevant OpenCode v1 frames (verified against ``opencode`` v1.17.17
``GET /doc`` and a captured live turn) are:

- ``message.part.delta {partID, field:"text", delta}`` -- one incremental chunk
  (the new chunk only, not a growing snapshot). ``field`` is ``"text"`` even for
  a reasoning part, so the channel is resolved from the part's snapshot (below)
  by ``partID``; only text-part chunks become ``TextBlock``s, so a reasoning
  model's thinking never leaks into ``text_delta`` / ``final``.
- ``message.part.updated {part}`` -- part snapshots carrying ``part.id`` and
  ``part.type`` (``text`` | ``reasoning`` | ``tool`` | ``step-*``). A ``tool`` part at
  ``state.status == "running"`` maps to one ``ToolUseBlock``; a ``step-finish``
  part carries that **step's** token block. The ``text`` part-updated snapshots
  (the empty text-start and full text-end bookends) are intentionally ignored so
  text already streamed via deltas is never emitted twice.
- ``message.updated {info}`` -- assistant message info carrying ``modelID`` and
  the latest step's ``tokens``.

Token accounting is **per step, not cumulative** (verified against a live
``opencode`` 1.17.17 multi-step turn): each ``step-finish`` part and each
``message.updated`` reports the output tokens of *that step only* and resets on
the next step, while ``session.updated`` carries the session-cumulative total
that spans turns. So a turn's true output total is the *sum* of its per-step
maxima, not a single running max over the reports. ``TurnSynthesizer`` therefore
accumulates one monotonic max per step (keyed by the ``step-finish`` part id,
which is genuinely per-step; ``message.updated``'s id is the turn-constant
message id and is only a fallback when no ``step-finish`` carries tokens) and
sums them. Folding those reports into one max instead undercounts a multi-step
turn (a budget ceiling between one step's tokens and the turn total would never
trip), which is the bug this module previously had.
- ``session.status {status:{type}}`` -- a ``busy`` -> ``idle`` transition marks
  turn completion (the terminal ``TurnResult``). ``session.idle`` is the
  deprecated equivalent and is handled the same way.
- ``session.error {error}`` -- a hard failure, mapped to an error result.
"""

from __future__ import annotations

from typing import Any

from ..events import AssistantText, ToolCall, TurnResult


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
) -> TurnResult:
    return TurnResult(
        text=text,
        is_error=is_error,
        subtype="error" if is_error else "success",
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
        self._model_id: str | None = None
        self._error: str | None = None
        self._seen_activity = False
        # Per-step output-token accounting. OpenCode reports output tokens PER
        # STEP -- each step-finish part and each message.updated carries only
        # that step's tokens and resets on the next step -- so the turn total is
        # the SUM of per-step maxima, not one running max over the reports.
        # ``_step_output`` keys the max seen per step-finish part id (a step's
        # block can still stream upward within one id, so keep the max);
        # ``_msg_output_max`` is the fallback total used only when no step-finish
        # part carries tokens (message.updated's id is the turn-constant message
        # id, so summing it alongside the step parts would double-count the same
        # per-step reports). ``_last_input`` is the most recent block's input
        # count, kept so the terminal result carries the real input alongside the
        # summed output. ``_emitted_output_total`` is the running total already
        # emitted as mid-turn deltas, so each carrier is the positive rise (never
        # negative, never double-counted).
        self._step_output: dict[str, int] = {}
        self._msg_output_max = 0
        self._last_input: int | None = None
        self._emitted_output_total = 0
        # partID -> part.type, learned from message.part.updated snapshots. A
        # message.part.delta carries field="text" even when its part is a
        # reasoning part, so the delta alone cannot tell the answer channel from
        # the thinking channel; the part's snapshot (always emitted before the
        # part's first delta, verified on the live wire) supplies the real type.
        self._part_types: dict[str, str] = {}

    def _running_output_total(self) -> int:
        """The turn's output total so far: the sum of per-step maxima.

        Falls back to the max output seen on ``message.updated`` only when no
        ``step-finish`` part carried tokens, so a turn whose only usage source is
        ``message.updated`` still reports something rather than nothing.
        """

        if self._step_output:
            return sum(self._step_output.values())
        return self._msg_output_max

    def _terminal_usage(self) -> dict[str, int] | None:
        """The terminal token block: last-seen input, summed per-step output."""

        if self._last_input is None:
            return None
        return {
            "input_tokens": self._last_input,
            "output_tokens": self._running_output_total(),
        }

    def _terminal(self) -> TurnResult:
        usage = self._terminal_usage()
        if self._error is not None:
            return _result(text=self._error, is_error=True, usage=usage)
        return _result(text=self._final_text, is_error=False, usage=usage)

    def _usage_carrier(self) -> AssistantText | None:
        """Emit the positive rise in the running turn total, or None if flat.

        Called after a per-step max is updated; the carrier delta is the amount
        the summed turn total rose since the last carrier, so the runner folds
        each step's tokens into the budget exactly once and never negatively.
        """

        total = self._running_output_total()
        if total <= self._emitted_output_total:
            return None
        delta = total - self._emitted_output_total
        self._emitted_output_total = total
        return AssistantText(
            text="",
            model=self._model_id or "",
            usage={"output_tokens": delta},
        )

    def ingest(self, frame: dict[str, Any]) -> list[Any]:
        if self.done:
            return []
        ftype = frame.get("type")
        props = frame.get("properties", {})
        if not isinstance(props, dict):
            return []
        out: list[Any] = []

        if ftype == "message.part.delta" and props.get("field") == "text":
            # Any text-channel delta means the turn is live, even one we drop.
            self._seen_activity = True
            chunk = props.get("delta", "")
            # Emit only content whose part is known to be a text part. Reasoning
            # deltas (and any delta for a partID not yet typed) are dropped so a
            # reasoning model's thinking never leaks into text_delta / final --
            # mirroring how translate.py drops non-TextBlock content. Deny by
            # default is safe because the part's start snapshot always precedes
            # its first delta (verified on the live wire); a missing snapshot
            # drops the chunk rather than leaking an untyped channel.
            if chunk and self._part_types.get(str(props.get("partID"))) == "text":
                self._final_text += chunk
                out.append(AssistantText(text=chunk, model=self._model_id or ""))

        elif ftype == "message.part.updated":
            part = props.get("part", {})
            if isinstance(part, dict):
                ptype = part.get("type")
                pid = part.get("id")
                if isinstance(pid, str) and isinstance(ptype, str):
                    self._part_types[pid] = ptype
                if ptype in ("text", "step-start", "step-finish", "reasoning"):
                    self._seen_activity = True
                if ptype == "tool":
                    state = part.get("state", {}) if isinstance(part.get("state"), dict) else {}
                    status = state.get("status")
                    if status == "running":
                        self._seen_activity = True
                        out.append(
                            ToolCall(
                                name=str(part.get("tool") or "unknown"),
                                id=str(part.get("callID") or part.get("id") or "call"),
                                model=self._model_id or "",
                            )
                        )
                    elif status == "error":
                        self._error = str(state.get("error") or "tool failed")
                elif ptype == "step-finish":
                    block = _tokens(part.get("tokens"))
                    if block is not None:
                        # Key this step's output by its part id (per-step on the
                        # wire); a missing id collapses to one bucket. A step's
                        # block can stream upward within the id, so keep the max.
                        key = pid if isinstance(pid, str) and pid else "__step__"
                        self._step_output[key] = max(
                            self._step_output.get(key, 0), block["output_tokens"]
                        )
                        self._last_input = block["input_tokens"]
                        carrier = self._usage_carrier()
                        if carrier is not None:
                            out.append(carrier)

        elif ftype == "message.updated":
            info = props.get("info", {})
            if isinstance(info, dict):
                model = info.get("modelID")
                if model:
                    self._model_id = str(model)
                block = _tokens(info.get("tokens"))
                if block is not None:
                    # message.updated reports the same per-step output as the
                    # step-finish part but under a turn-constant message id, so
                    # it cannot be a per-step key without double-counting. Track
                    # its max as a fallback total, and only let it drive the
                    # last-seen block and mid-turn carrier when no step-finish
                    # part has supplied tokens.
                    self._msg_output_max = max(
                        self._msg_output_max, block["output_tokens"]
                    )
                    if not self._step_output:
                        self._last_input = block["input_tokens"]
                        carrier = self._usage_carrier()
                        if carrier is not None:
                            out.append(carrier)

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
