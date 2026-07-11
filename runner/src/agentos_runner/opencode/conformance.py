"""Run the frozen ACI conformance suite over a LIVE OpenCode-backed runner.

Mirrors ``agentos_runner.conformance`` but swaps the fake session for the live
:class:`OpenCodeModelSession`, so the gate validates the real translation/final
plumbing against a genuinely running ``opencode serve`` and real model turns --
not a scripted stream. ``run_conformance`` drives four inbound cases (message,
job, eval_case, interrupt); the three events each cost one real model turn, and
the bare interrupt yields an idle final without a model call. Each producer call
builds an isolated runner (fresh subprocess) to completion via ``anyio.run``.

Run:  PYTHONPATH=src python -m agentos_runner.opencode.conformance   (from runner/)
"""

from __future__ import annotations

import anyio
from aci_protocol import Event, Interrupt, run_conformance

from ..otel import RunTracer
from ..session import SessionRunner
from ..side_effects import SideEffectClassifier
from .installer import OpenCodeBundleInstaller
from .session import OPENCODE_READONLY_TOOLS, OpenCodeModelSession


def _build_runner() -> SessionRunner:
    # The conformance session is deliberately bundle-less: the stub installer
    # makes that an explicit decision at the port rather than an accident (#310).
    OpenCodeBundleInstaller().install(None)
    return SessionRunner(
        session_factory=OpenCodeModelSession,
        ceiling=0,  # unbounded: conformance validates protocol shape, not budgets
        tracer=RunTracer(None),
        classifier=SideEffectClassifier(OPENCODE_READONLY_TOOLS),
        trace_name="opencode-conformance",
    )


def opencode_conformance_producer(message: Event | Interrupt) -> list[str]:
    """Return the NDJSON lines a live OpenCode runner emits for one inbound frame."""

    lines: list[str] = []

    async def _run() -> None:
        runner = _build_runner()
        await runner.start()
        try:
            async for line in runner.run_inbound(message):
                lines.append(line)
        finally:
            await runner.close()

    anyio.run(_run)
    return lines


def _demo_turn(prompt: str = "Reply with exactly the word: pong") -> list[str]:
    """Drive one live turn and return its ACI NDJSON lines."""

    lines: list[str] = []

    async def _run() -> None:
        runner = _build_runner()
        await runner.start()
        try:
            event = Event(type="message", text=prompt, user="U1", ts="1.0")
            async for line in runner.run_turn(event):
                lines.append(line)
        finally:
            await runner.close()

    anyio.run(_run)
    return lines


def main() -> int:
    print("=== OpenCode-backed ACI conformance (live opencode serve) ===")
    report = run_conformance(opencode_conformance_producer)
    for check in report.checks:
        print(f"  [{'PASS' if check.passed else 'FAIL'}] {check.name}: {check.detail}")
    print(f"  -> {report.summary()}  passed={report.passed}")

    print("\n=== Representative live OpenCode turn -> ACI NDJSON ===")
    demo = _demo_turn()
    for line in demo:
        print("  " + line.rstrip())

    ends_in_final = bool(demo) and '"final"' in demo[-1]
    ok = report.passed and ends_in_final
    print(f"\nLIVE SPIKE RESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
