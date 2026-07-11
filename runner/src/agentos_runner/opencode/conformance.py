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

import atexit
import json
import os
import tempfile

import anyio
from aci_protocol import Event, Interrupt, run_conformance

from ..otel import RunTracer
from ..session import SessionRunner
from ..side_effects import SideEffectClassifier
from .installer import OpenCodeBundleInstaller
from .session import OPENCODE_READONLY_TOOLS, OpenCodeModelSession

_DEFAULT_DEMO_PROMPT = "Reply with exactly the word: pong"

# When a bundle is installed, the representative turn must exercise it end to end:
# load a skill via the skill tool and call the bundled MCP tool, proving the
# compiled config reached OpenCode (issue #310 live round-trip proof).
_BUNDLE_DEMO_PROMPT = (
    "First use the skill tool to load the 'roundtrip-greeter' skill and follow "
    "its instruction, then call the 'agentos_probe' MCP tool and include its "
    "output verbatim in your reply."
)

# Expected proof values from runner/tests/fixtures/opencode_live_bundle.
_BUNDLE_SKILL_TOKEN = "AGENTOS-ROUNDTRIP"
_BUNDLE_MCP_NONCE = "NONCE-7f3a9c"

_CONFORMANCE_WORKDIR: tempfile.TemporaryDirectory[str] | None = None


def _conformance_workdir_root() -> str:
    """Return the shared root for compiled bundles in this process."""

    global _CONFORMANCE_WORKDIR
    if _CONFORMANCE_WORKDIR is None:
        _CONFORMANCE_WORKDIR = tempfile.TemporaryDirectory(
            prefix="agentos-opencode-conformance-"
        )
        atexit.register(_CONFORMANCE_WORKDIR.cleanup)
    return _CONFORMANCE_WORKDIR.name


def _bundle_demo_failures(lines: list[str]) -> list[str]:
    """Return missing proof from a bundle demo turn."""

    parsed: list[object] = []
    for line in lines:
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            parsed.append(None)

    failures: list[str] = []
    terminal = parsed[-1] if parsed else None
    if (
        not isinstance(terminal, dict)
        or terminal.get("type") != "final"
        or terminal.get("status") != "done"
    ):
        failures.append("no final/done")
    else:
        text = terminal.get("text")
        final_text = text if isinstance(text, str) else ""
        if _BUNDLE_SKILL_TOKEN not in final_text:
            failures.append("missing skill token")
        if _BUNDLE_MCP_NONCE not in final_text:
            failures.append("missing nonce")

    has_skill_note = any(
        isinstance(event, dict)
        and event.get("type") == "tool_note"
        and event.get("tool") == "skill"
        for event in parsed
    )
    if not has_skill_note:
        failures.append("no skill tool_note")
    return failures


def _build_runner(plugin_dir: str | None) -> SessionRunner:
    # Compile the bundle (if any) and bind the materialized workdir as the
    # session's cwd -- this is where OpenCode discovers the compiled config.
    # A bundle-less call yields cwd=None (the session mkdtemps its own workdir).
    compiled = OpenCodeBundleInstaller(
        workdir_root=_conformance_workdir_root()
    ).install(plugin_dir)
    cwd = compiled.workdir if compiled else None
    return SessionRunner(
        session_factory=lambda: OpenCodeModelSession(cwd=cwd),
        ceiling=0,  # unbounded: conformance validates protocol shape, not budgets
        tracer=RunTracer(None),
        classifier=SideEffectClassifier(OPENCODE_READONLY_TOOLS),
        trace_name="opencode-conformance",
    )


def opencode_conformance_producer(message: Event | Interrupt) -> list[str]:
    """Return the NDJSON lines a live OpenCode runner emits for one inbound frame."""

    lines: list[str] = []

    async def _run() -> None:
        runner = _build_runner(os.environ.get("AGENTOS_PLUGIN_DIR"))
        await runner.start()
        try:
            async for line in runner.run_inbound(message):
                lines.append(line)
        finally:
            await runner.close()

    anyio.run(_run)
    return lines


def _demo_turn(prompt: str) -> list[str]:
    """Drive one live turn and return its ACI NDJSON lines."""

    lines: list[str] = []

    async def _run() -> None:
        runner = _build_runner(os.environ.get("AGENTOS_PLUGIN_DIR"))
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
    plugin_dir = os.environ.get("AGENTOS_PLUGIN_DIR")
    print("=== OpenCode-backed ACI conformance (live opencode serve) ===")
    if plugin_dir:
        print(f"  (bundle installed from AGENTOS_PLUGIN_DIR={plugin_dir})")
    report = run_conformance(opencode_conformance_producer)
    for check in report.checks:
        print(f"  [{'PASS' if check.passed else 'FAIL'}] {check.name}: {check.detail}")
    print(f"  -> {report.summary()}  passed={report.passed}")

    print("\n=== Representative live OpenCode turn -> ACI NDJSON ===")
    demo = _demo_turn(_BUNDLE_DEMO_PROMPT if plugin_dir else _DEFAULT_DEMO_PROMPT)
    for line in demo:
        print("  " + line.rstrip())

    if plugin_dir:
        bundle_failures = _bundle_demo_failures(demo)
        for failure in bundle_failures:
            print(f"  [FAIL] bundle demo: {failure}")
        ok = report.passed and not bundle_failures
    else:
        ends_in_final = bool(demo) and '"final"' in demo[-1]
        ok = report.passed and ends_in_final
    print(f"\nLIVE SPIKE RESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
