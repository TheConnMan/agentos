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
from .version import PROTOCOL_VERSION, is_compatible

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
        return "incompatible version rejected with ProtocolVersionError"
    raise AssertionError("a 9.9.9 version line was accepted; it must be rejected")


def _accept_compatible_patch_version() -> str:
    # A same-major-minor patch difference is not an error: a consumer speaking
    # PROTOCOL_VERSION accepts a producer one patch ahead.
    major, minor, patch = (int(p) for p in PROTOCOL_VERSION.split("."))
    ahead = f"{major}.{minor}.{patch + 1}"
    if not is_compatible(ahead, PROTOCOL_VERSION):  # pragma: no cover - guards the sample
        raise AssertionError(f"{ahead} should be compatible with {PROTOCOL_VERSION}")
    line = json.dumps({"type": "final", "version": ahead, "text": "x", "status": "done"})
    decoded = parse_ndjson_line(line)
    if decoded.version != ahead:
        raise AssertionError(f"a compatible {ahead} line did not round-trip its version")
    return f"compatible patch version {ahead} accepted"


def _tolerate_unknown_field() -> str:
    # Consumers accept and ignore unknown fields they do not model.
    line = json.dumps(
        {
            "type": "final",
            "version": PROTOCOL_VERSION,
            "text": "x",
            "status": "done",
            "future_field": 1,
        }
    )
    decoded = parse_ndjson_line(line)
    if not isinstance(decoded, Final) or decoded.text != "x":
        raise AssertionError("an unknown field was not tolerated on read")
    return "unknown field tolerated on read"


def _reject_missing_version() -> str:
    bad = json.dumps({"type": "final", "text": "x", "status": "done"})
    try:
        parse_ndjson_line(bad)
    except ProtocolVersionError:
        return "missing version rejected with ProtocolVersionError"
    raise AssertionError("a version-less line was accepted; it must be rejected")


def _producer_stream(producer: Producer) -> str:
    # Exercise every inbound case (message, job, eval_case, interrupt): a runner
    # that mishandles interrupt or a batch job must not pass the gate by handling
    # only ordinary messages.
    for message in _INBOUND_SAMPLES:
        label = message.kind if isinstance(message, Interrupt) else f"event:{message.type}"
        events = parse_ndjson("".join(producer(message)))
        if not events:
            raise AssertionError(f"producer emitted no events for {label}")
        if events[-1].type != "final":
            raise AssertionError(
                f"producer stream for {label} must end in a final event, "
                f"ended in {events[-1].type!r}"
            )
        for event in events:
            if not is_compatible(event.version, PROTOCOL_VERSION):
                raise AssertionError(f"producer emitted version {event.version!r} for {label}")
    return f"producer streams for {len(_INBOUND_SAMPLES)} inbound cases are well formed"


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
        _check("accept_compatible_patch_version", _accept_compatible_patch_version),
        _check("tolerate_unknown_field", _tolerate_unknown_field),
    ]
    if producer is not None:
        checks.append(_check("producer_stream", lambda: _producer_stream(producer)))
    return ConformanceReport(passed=all(c.passed for c in checks), checks=checks)
