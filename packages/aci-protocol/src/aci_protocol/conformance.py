"""A reusable conformance suite for ACI runner implementations.

A runner is conformant if it round-trips every outbound event type through
NDJSON, rejects events declaring an unknown protocol version, round-trips inbound
messages, and emits a well formed stream for an inbound event. The library level
checks (round-trips, version rejection) always run; passing a ``producer``
additionally validates a real implementation's output stream.

D1 imports ``run_conformance`` and passes its runner's NDJSON producer; the
package's own tests pass ``reference_producer``. The report is a plain data
object so a caller (pytest, a CLI, CI) can assert on it however it likes.
"""

import json
from collections.abc import Callable, Iterable

from pydantic import BaseModel

from .events import (
    ErrorEvent,
    Event,
    Final,
    Interrupt,
    SessionStatus,
    SideEffectFlag,
    TextDelta,
    ToolNote,
)
from .ndjson import (
    OutboundEventModel,
    ProtocolVersionError,
    parse_inbound,
    parse_ndjson,
    parse_ndjson_line,
    to_inbound_json,
    to_ndjson_line,
)
from .version import PROTOCOL_VERSION

# A producer maps an inbound message to the NDJSON lines a runner emits for it.
Producer = Callable[[Event | Interrupt], Iterable[str]]

# One canonical instance of every outbound event type, exercised by the
# round-trip check so no event shape is left unverified.
_OUTBOUND_SAMPLES: tuple[OutboundEventModel, ...] = (
    TextDelta(text="partial output"),
    ToolNote(text="calling a tool", tool="search"),
    Final(text="final answer", status=SessionStatus.DONE),
    ErrorEvent(message="something failed", classification="model-error"),
    SideEffectFlag(tool="send_email", detail="one email sent"),
)

_INBOUND_SAMPLES: tuple[Event | Interrupt, ...] = (
    Event(type="message", text="hello there", user="U123", ts="1720200000.000100"),
    Event(type="job", text="nightly report", user="cron", ts="1720200001.000200"),
    Event(type="eval_case", text="case 7", user="eval", ts="1720200002.000300"),
    Interrupt(reason="user pressed stop"),
)


class CheckResult(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class ConformanceReport(BaseModel):
    passed: bool
    checks: list[CheckResult]

    def summary(self) -> str:
        good = sum(1 for c in self.checks if c.passed)
        return f"{good}/{len(self.checks)} conformance checks passed"


def _check(name: str, fn: Callable[[], str]) -> CheckResult:
    try:
        detail = fn()
        return CheckResult(name=name, passed=True, detail=detail)
    except Exception as exc:  # noqa: BLE001 - the suite reports failures, never raises
        return CheckResult(name=name, passed=False, detail=f"{type(exc).__name__}: {exc}")


def _roundtrip_outbound() -> str:
    for sample in _OUTBOUND_SAMPLES:
        decoded = parse_ndjson_line(to_ndjson_line(sample))
        if decoded != sample:
            raise AssertionError(f"round-trip mismatch for {sample.type!r}")
    return f"{len(_OUTBOUND_SAMPLES)} outbound event types round-tripped"


def _roundtrip_inbound() -> str:
    for sample in _INBOUND_SAMPLES:
        decoded = parse_inbound(to_inbound_json(sample))
        if decoded != sample:
            raise AssertionError(f"inbound round-trip mismatch for {sample.kind!r}")
    return f"{len(_INBOUND_SAMPLES)} inbound messages round-tripped"


def _reject_unknown_version() -> str:
    bad = json.dumps({"type": "final", "version": "9.9.9", "text": "x", "status": "done"})
    try:
        parse_ndjson_line(bad)
    except ProtocolVersionError:
        return "unknown version rejected with ProtocolVersionError"
    raise AssertionError("a 9.9.9 version line was accepted; it must be rejected")


def _reject_missing_version() -> str:
    bad = json.dumps({"type": "final", "text": "x", "status": "done"})
    try:
        parse_ndjson_line(bad)
    except ProtocolVersionError:
        return "missing version rejected with ProtocolVersionError"
    raise AssertionError("a version-less line was accepted; it must be rejected")


def _producer_stream(producer: Producer) -> str:
    message = Event(type="message", text="do the thing", user="U1", ts="1720200003.0004")
    lines = list(producer(message))
    document = "".join(lines)
    events = parse_ndjson(document)
    if not events:
        raise AssertionError("producer emitted no events for an inbound message")
    if events[-1].type != "final":
        raise AssertionError(
            f"producer stream must end in a final event, ended in {events[-1].type!r}"
        )
    for event in events:
        if event.version != PROTOCOL_VERSION:
            raise AssertionError(f"producer emitted version {event.version!r}")
    return f"producer stream of {len(events)} events is well formed"


def run_conformance(producer: Producer | None = None) -> ConformanceReport:
    """Run the conformance checks and return a report.

    Pass ``producer`` (a runner's inbound-to-NDJSON function) to additionally
    validate a real implementation's output stream. Never raises: every failure
    is captured as a failing CheckResult.
    """

    checks = [
        _check("outbound_roundtrip", _roundtrip_outbound),
        _check("inbound_roundtrip", _roundtrip_inbound),
        _check("reject_unknown_version", _reject_unknown_version),
        _check("reject_missing_version", _reject_missing_version),
    ]
    if producer is not None:
        checks.append(_check("producer_stream", lambda: _producer_stream(producer)))
    return ConformanceReport(passed=all(c.passed for c in checks), checks=checks)
