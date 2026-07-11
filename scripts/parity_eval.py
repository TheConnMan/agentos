#!/usr/bin/env python3
"""Capture driver for the #313 OpenCode-vs-Claude parity evals (plan §3.1).

This is dev tooling, not shipped product code. It drives one frozen-schema eval
suite through a runner container over the ACI HTTP channel, grades each case with
semantics byte-identical to the platform's ``EvalRunner``, and captures the
normalized per-turn metadata the frozen wire does not carry (tokens, model) from
the runner's own OTel ``gen_ai`` spans via a tiny embedded OTLP/HTTP receiver.

Only stdlib + workspace deps are used (``agentos_worker.eval.models``,
``agentos_worker.runner_client``, ``aiohttp``, ``opentelemetry-proto`` -- already
a transitive dep of the runner's exporter). No new dependencies.

The pure helpers (``compute_usd``, ``grade_frames``, ``match_spans_to_cases``,
aggregation, delta-table rendering) are top-level functions so the offline unit
tests (``apps/worker/tests/eval/test_parity_suite.py``) can exercise them without
touching the network. All live I/O runs under ``asyncio.run(main())``.

Usage (one command per suite run, driver-invoked):

    uv run python scripts/parity_eval.py \\
      --suite docs/evals/opencode-parity/cases.json \\
      --target http://localhost:18080 \\
      --arm A --rep 1 --model claude-sonnet-5 --provider anthropic \\
      --otlp-port 4318 \\
      --out docs/evals/opencode-parity/results/armA-rep1.jsonl \\
      --prices prices.json

Modes: default (capture a graded suite run), ``--probe steer|budget`` (the §1.5
behavioral probes appended to ``probes.jsonl``), ``--coldstart`` (poll ``/healthz``
readiness), and ``--render`` (regenerate the readout tables from committed raw
files so tables and raw data can never disagree).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from statistics import median
from typing import Any

import aiohttp
from aci_protocol import (
    ErrorEvent,
    Event,
    Final,
    SessionStatus,
    SideEffectFlag,
    TextDelta,
    ToolNote,
)
from agentos_worker.eval.models import EvalSuite, Grader
from agentos_worker.runner_client import RunnerClient, RunnerError
from aiohttp import web
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

# The literal string a missing/empty/zero usage capture reports. Never 0.0 --
# a silent zero would poison the readout's cost column (plan §3.1).
UNAVAILABLE = "unavailable"

# The runner exports the generation span under this name (runner/src/agentos_runner/otel.py).
_GENERATION_SPAN_NAME = "llm.generation"
_SESSION_ID_ATTR = "agentos.session_id"


# --- Pure helpers (unit-tested) ----------------------------------------------


def compute_usd(usage: dict[str, Any], prices: dict[str, float]) -> float | str:
    """List-price-equivalent USD for one turn, or ``"unavailable"``.

    ``usd = in_tokens * prices["input"] + out_tokens * prices["output"]``. When
    the usage map carries no real token counts -- empty, absent, or an explicit
    both-zero -- the capture failed and this returns the literal string
    ``"unavailable"`` (never ``0.0``, the silent-zero failure mode the readout's
    cost column must never absorb).
    """
    in_tokens = usage.get("input_tokens")
    out_tokens = usage.get("output_tokens")
    if not isinstance(in_tokens, int) or not isinstance(out_tokens, int):
        return UNAVAILABLE
    if in_tokens <= 0 and out_tokens <= 0:
        return UNAVAILABLE
    return in_tokens * prices["input"] + out_tokens * prices["output"]


def grade_frames(frames: Sequence[Any], grader: Grader) -> bool:
    """Grade a turn's frames exactly as ``EvalRunner._run_case`` does.

    Mirrors ``apps/worker/src/agentos_worker/eval/runner.py`` byte-for-byte: the
    graded output is the ``Final`` text when a final arrived, else the joined
    ``TextDelta`` text; a ``CLASSIFIED_FAILURE`` final can never grade green
    regardless of the grader; otherwise the grader decides.
    """
    parts: list[str] = []
    final_text: str | None = None
    final_status: SessionStatus | None = None
    for frame in frames:
        if isinstance(frame, TextDelta):
            parts.append(frame.text)
        elif isinstance(frame, Final):
            final_text = frame.text
            final_status = frame.status

    output = final_text if final_text is not None else "".join(parts)
    if final_status is SessionStatus.CLASSIFIED_FAILURE:
        return False
    return grader.grade(output)


def grade_case(frames: Sequence[Any], grader: Grader) -> bool:
    """Grade one consumed turn exactly as ``EvalRunner._run_case`` does end to end.

    ``grade_frames`` covers the normal + classified-failure paths. This wrapper
    adds the transport-exception path: when ``_consume_case`` catches a
    ``RunnerError``/``ClientError``/``TimeoutError`` it returns ``frames == []``,
    and ``runner.py:60-67`` returns ``passed=False`` *unconditionally* there --
    the grader never runs. Empty frames uniquely identify that path (a normal turn
    always yields at least a ``Final``), so an empty frame list grades ``False``
    regardless of the grader, byte-identical to ``EvalRunner``.
    """
    if not frames:
        return False
    return grade_frames(frames, grader)


def match_spans_to_cases(
    spans: Sequence[dict[str, Any]], case_ids: Sequence[str], session_id: str
) -> list[tuple[str, dict[str, Any]]]:
    """Pair case *i* with span *i* in arrival order, cross-checking session id.

    Cases run sequentially and the runner exports one generation span per turn
    via ``SimpleSpanProcessor``, so span arrival order matches case order. Raises
    ``ValueError`` loudly on a count mismatch or any span whose
    ``agentos.session_id`` disagrees with ``session_id`` -- never a silent
    mis-attribution of tokens to the wrong run.
    """
    if len(spans) != len(case_ids):
        raise ValueError(
            f"span/case count mismatch: {len(spans)} spans for {len(case_ids)} cases "
            f"(session {session_id!r}) -- capture failed, re-run this rep"
        )
    for span in spans:
        span_session = span.get(_SESSION_ID_ATTR)
        if span_session != session_id:
            raise ValueError(
                f"span session {span_session!r} disagrees with expected {session_id!r} "
                "-- spans are from another run, refusing to mis-attribute"
            )
    return [(case_id, span) for case_id, span in zip(case_ids, spans, strict=True)]


# --- OTLP span decoding ------------------------------------------------------


def _any_value(value: Any) -> Any:
    """Unwrap an OTLP ``AnyValue`` into a plain Python scalar."""
    which = value.WhichOneof("value")
    if which is None:
        return None
    return getattr(value, which)


def _attrs_to_dict(attributes: Any) -> dict[str, Any]:
    return {kv.key: _any_value(kv.value) for kv in attributes}


def generation_spans_from_request(
    request: ExportTraceServiceRequest,
) -> list[dict[str, Any]]:
    """Extract one flat dict per ``llm.generation`` span in the export request.

    Each dict merges the resource's ``agentos.session_id`` with the span's
    ``gen_ai.*`` attributes, keyed for downstream use: ``agentos.session_id``,
    ``input_tokens``, ``output_tokens``, ``model``.
    """
    out: list[dict[str, Any]] = []
    for resource_spans in request.resource_spans:
        resource_attrs = _attrs_to_dict(resource_spans.resource.attributes)
        session_id = resource_attrs.get(_SESSION_ID_ATTR)
        for scope_spans in resource_spans.scope_spans:
            for span in scope_spans.spans:
                if span.name != _GENERATION_SPAN_NAME:
                    continue
                span_attrs = _attrs_to_dict(span.attributes)
                record: dict[str, Any] = {_SESSION_ID_ATTR: session_id}
                in_tokens = span_attrs.get("gen_ai.usage.input_tokens")
                out_tokens = span_attrs.get("gen_ai.usage.output_tokens")
                model = span_attrs.get("gen_ai.request.model")
                if isinstance(in_tokens, int):
                    record["input_tokens"] = in_tokens
                if isinstance(out_tokens, int):
                    record["output_tokens"] = out_tokens
                if isinstance(model, str):
                    record["model"] = model
                out.append(record)
    return out


class OtlpReceiver:
    """Embedded OTLP/HTTP receiver collecting the runner's generation spans.

    Listens on ``POST /v1/traces`` (Content-Type application/x-protobuf), decodes
    ``ExportTraceServiceRequest``, and accumulates one flat span dict per
    ``llm.generation`` span in arrival order.
    """

    def __init__(self, port: int) -> None:
        self._port = port
        self.spans: list[dict[str, Any]] = []
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/v1/traces", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self._port)
        await site.start()

    async def _handle(self, request: web.Request) -> web.Response:
        body = await request.read()
        export = ExportTraceServiceRequest()
        export.ParseFromString(body)
        self.spans.extend(generation_spans_from_request(export))
        return web.Response(
            body=ExportTraceServiceResponse().SerializeToString(),
            content_type="application/x-protobuf",
        )

    async def wait_for_spans(self, expected: int, timeout_s: float = 30.0) -> None:
        """Block until ``expected`` spans have arrived, else raise loudly."""
        deadline = time.monotonic() + timeout_s
        while len(self.spans) < expected:
            if time.monotonic() >= deadline:
                raise ValueError(
                    f"timed out waiting for {expected} generation spans "
                    f"(got {len(self.spans)}) -- tokens unavailable, re-run this rep"
                )
            await asyncio.sleep(0.1)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()


# --- Live capture ------------------------------------------------------------


async def _consume_case(
    client: RunnerClient, target: str, case_input: str
) -> dict[str, Any]:
    """Send one ``eval_case`` event and consume the full stream into a record."""
    event = Event(type="eval_case", text=case_input, user="eval", ts="0")
    parts: list[str] = []
    final_text: str | None = None
    final_status: SessionStatus | None = None
    error_classification: str | None = None
    tool_names: list[str] = []
    side_effect_tools: list[str] = []
    frames: list[Any] = []

    start = time.monotonic()
    try:
        turn = await client.start_turn(target, event)
        async with turn:
            async for frame in turn:
                frames.append(frame)
                if isinstance(frame, TextDelta):
                    parts.append(frame.text)
                elif isinstance(frame, Final):
                    final_text = frame.text
                    final_status = frame.status
                elif isinstance(frame, ErrorEvent):
                    error_classification = frame.classification or frame.message
                elif isinstance(frame, ToolNote):
                    tool_names.append(frame.tool or frame.text)
                elif isinstance(frame, SideEffectFlag):
                    side_effect_tools.append(frame.tool or "")
    except (RunnerError, aiohttp.ClientError, TimeoutError) as exc:
        return {
            "frames": [],
            "latency_ms": round((time.monotonic() - start) * 1000, 2),
            "final_text": "",
            "delta_text": "".join(parts),
            "final_status": None,
            "error_classification": str(exc),
            "tool_notes": {"count": 0, "tools": []},
            "side_effect_flags": {"count": 0, "tools": []},
            "empty_final": True,
        }

    latency_ms = round((time.monotonic() - start) * 1000, 2)
    delta_text = "".join(parts)
    output = final_text if final_text is not None else delta_text
    return {
        "frames": frames,
        "latency_ms": latency_ms,
        "final_text": output,
        # The joined pre-Final streamed deltas, kept so probes can honestly
        # report whether any partial answer text leaked before enforcement.
        "delta_text": delta_text,
        "final_status": final_status.value if final_status is not None else None,
        "error_classification": error_classification,
        "tool_notes": {"count": len(tool_names), "tools": tool_names},
        "side_effect_flags": {
            "count": len(side_effect_tools),
            "tools": side_effect_tools,
        },
        "empty_final": output == "",
    }


async def run_suite(args: argparse.Namespace) -> int:
    """Capture one graded suite run for (arm, rep) and write the JSONL out."""
    suite = EvalSuite.model_validate_json(Path(args.suite).read_text(encoding="utf-8"))
    prices = json.loads(Path(args.prices).read_text(encoding="utf-8"))
    session_id = f"parity-{args.arm}-{args.rep}"

    receiver = OtlpReceiver(args.otlp_port)
    await receiver.start()

    case_records: list[dict[str, Any]] = []
    try:
        async with RunnerClient() as client:
            for case in suite.cases:
                consumed = await _consume_case(client, args.target, case.input)
                passed = grade_case(consumed["frames"], case.grader)
                record = {k: v for k, v in consumed.items() if k != "frames"}
                record.update(
                    {
                        "case_id": case.id,
                        "arm": args.arm,
                        "rep": args.rep,
                        "model": args.model,
                        "provider": args.provider,
                        "passed": passed,
                    }
                )
                case_records.append(record)

        # Attribute per-turn spans to cases by arrival order, cross-checked
        # against the session id. A capture failure raises loudly here.
        await receiver.wait_for_spans(len(suite.cases))
        pairs = match_spans_to_cases(
            receiver.spans, [c.id for c in suite.cases], session_id
        )
    finally:
        await receiver.stop()

    span_by_case = dict(pairs)
    for record in case_records:
        span = span_by_case[record["case_id"]]
        usage = {
            "input_tokens": span.get("input_tokens"),
            "output_tokens": span.get("output_tokens"),
        }
        record["input_tokens"] = span.get("input_tokens")
        record["output_tokens"] = span.get("output_tokens")
        record["otel_model"] = span.get("model")
        record["usd"] = compute_usd(usage, prices)

    _write_jsonl(Path(args.out), case_records)
    print(f"wrote {len(case_records)} case rows -> {args.out}")
    return 0


async def run_probe(args: argparse.Namespace) -> int:
    """Run a §1.5 behavioral probe and append its record to ``probes.jsonl``."""
    suite = EvalSuite.model_validate_json(Path(args.suite).read_text(encoding="utf-8"))
    out_path = Path(args.out)

    async with RunnerClient() as client:
        if args.probe == "steer":
            record = await _steer_probe(client, args.target, suite, args.arm, args.rep)
        else:  # budget
            record = await _budget_probe(client, args.target, suite, args.arm, args.rep)

    _append_jsonl(out_path, [record])
    print(f"appended {args.probe} probe ({args.arm}/{args.rep}) -> {out_path}")
    return 0


async def _steer_probe(
    client: RunnerClient, target: str, suite: EvalSuite, arm: str, rep: int
) -> dict[str, Any]:
    """Start a long turn, steer mid-turn, record whether it influenced this turn.

    Observes the completion-deferred-steer gap: Claude applies a mid-turn steer to
    the current turn, OpenCode surfaces it as the next turn's prompt.
    """
    long_prompt = (
        "Count slowly from 1 to 40, one number per line, pausing between each."
    )
    steer_text = "STEER-MARKER: also include the word PINEAPPLE in your reply."
    event = Event(type="eval_case", text=long_prompt, user="eval", ts="0")

    deltas: list[str] = []
    final_text: str | None = None
    steered = False
    turn = await client.start_turn(target, event)
    async with turn:
        async for frame in turn:
            if isinstance(frame, TextDelta):
                deltas.append(frame.text)
                if not steered:
                    steered = await client.steer(
                        target, Event(type="message", text=steer_text, user="eval", ts="0")
                    )
            elif isinstance(frame, Final):
                final_text = frame.text
    # Accumulate deltas and the terminal Final SEPARATELY, then define the
    # observed output with the same final-over-deltas semantics grading uses.
    # Appending Final onto the deltas would double-count a canonical Final that
    # echoes the streamed content (a bogus "the text repeats twice" artifact).
    delta_text = "".join(deltas)
    output = final_text if final_text is not None else delta_text
    return {
        "probe": "steer",
        "arm": arm,
        "rep": rep,
        "steer_accepted": steered,
        "influenced_current_turn": "PINEAPPLE" in output,
        "delta_excerpt": delta_text[:2000],
        "output_excerpt": output[:2000],
    }


async def _budget_probe(
    client: RunnerClient, target: str, suite: EvalSuite, arm: str, rep: int
) -> dict[str, Any]:
    """Re-run one case; the caller sets a tiny per-run budget on the container.

    Records the error+final classified-failure pair the runner must emit when the
    per-run output-token cap trips (plan §1.5). The tiny budget is injected by the
    driver's ``docker run`` (``AGENTOS_BUDGET``), not by this script.
    """
    case = suite.cases[-1]  # the multi-step case leaks the most output before halt
    consumed = await _consume_case(client, target, case.input)
    return {
        "probe": "budget",
        "arm": arm,
        "rep": rep,
        "case_id": case.id,
        "final_status": consumed["final_status"],
        "error_classification": consumed["error_classification"],
        "final_text_len": len(consumed["final_text"]),
        "delta_text_len": len(consumed["delta_text"]),
        "output_excerpt": consumed["final_text"][:2000],
        # The streamed pre-Final deltas -- how much partial answer, if any,
        # leaked before the per-run output-token cap tripped.
        "delta_excerpt": consumed["delta_text"][:2000],
    }


async def run_coldstart(args: argparse.Namespace) -> int:
    """Poll ``/healthz`` at 100ms, record the readiness window in ms.

    With ``--start-ts`` (an epoch captured by the caller *before* ``docker run``),
    ready-ms is measured from container start -> first ``/healthz`` 200 -- a true
    cold-start number (plan §3.2). Without it, the value is poll-only: the window
    from this poller starting to the first 200, which excludes container start.
    """
    poll_start = time.monotonic()
    deadline = poll_start + args.coldstart_timeout_s
    ready_ms: float | None = None
    from_container_start = args.start_ts is not None
    measured_from = (
        "container-start" if from_container_start else "poll-only (excludes container start)"
    )
    async with aiohttp.ClientSession() as session:
        while time.monotonic() < deadline:
            try:
                async with session.get(f"{args.target}/healthz") as resp:
                    if resp.status == 200:
                        if from_container_start:
                            ready_ms = round((time.time() - args.start_ts) * 1000, 2)
                        else:
                            ready_ms = round((time.monotonic() - poll_start) * 1000, 2)
                        break
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.1)

    record = {
        "probe": "coldstart",
        "arm": args.arm,
        "rep": args.rep,
        "ready_ms": ready_ms,
        "measured_from": measured_from,
        "timed_out": ready_ms is None,
    }
    _append_jsonl(Path(args.out), [record])
    if ready_ms is None:
        print(f"coldstart TIMED OUT after {args.coldstart_timeout_s}s", file=sys.stderr)
        return 1
    print(f"coldstart {args.arm}/{args.rep}: {ready_ms} ms ({measured_from}) -> {args.out}")
    return 0


# --- Aggregation + rendering -------------------------------------------------


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def aggregate_case(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll per-rep rows for one (arm, case) into the readout's median cells."""
    latencies = [v for v in (_num(r.get("latency_ms")) for r in rows) if v is not None]
    in_toks = [v for v in (_num(r.get("input_tokens")) for r in rows) if v is not None]
    out_toks = [v for v in (_num(r.get("output_tokens")) for r in rows) if v is not None]
    usds = [v for v in (_num(r.get("usd")) for r in rows) if v is not None]
    return {
        "passed": sum(1 for r in rows if r.get("passed")),
        "n": len(rows),
        "median_latency_ms": round(median(latencies), 2) if latencies else None,
        "median_input_tokens": round(median(in_toks)) if in_toks else None,
        "median_output_tokens": round(median(out_toks)) if out_toks else None,
        "median_usd": round(median(usds), 6) if usds else None,
        "tool_calls": sum(r.get("tool_notes", {}).get("count", 0) for r in rows),
        "side_effect_flags": sum(
            r.get("side_effect_flags", {}).get("count", 0) for r in rows
        ),
    }


def _cell(value: Any) -> str:
    return UNAVAILABLE if value is None else str(value)


def render_case_table(case_id: str, per_arm: dict[str, dict[str, Any]]) -> str:
    """One §5.2 per-use-case matrix table for a single case."""
    lines = [
        f"#### `{case_id}`",
        "",
        "| arm | pass k/n | median latency ms | tokens in/out (median) "
        "| USD (median) | tool calls | side-effect flags |",
        "|---|---|---|---|---|---|---|",
    ]
    for arm in sorted(per_arm):
        agg = per_arm[arm]
        toks = f"{_cell(agg['median_input_tokens'])}/{_cell(agg['median_output_tokens'])}"
        lines.append(
            f"| {arm} | {agg['passed']}/{agg['n']} | {_cell(agg['median_latency_ms'])} "
            f"| {toks} | {_cell(agg['median_usd'])} | {agg['tool_calls']} "
            f"| {agg['side_effect_flags']} |"
        )
    lines.append("")
    return "\n".join(lines)


def _load_results(results_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(results_dir.glob("arm*-rep*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def render_readout_tables(results_dir: Path) -> str:
    """Regenerate the §5.2 per-use-case tables from committed result files."""
    rows = _load_results(results_dir)
    by_case: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for row in rows:
        by_case.setdefault(row["case_id"], {}).setdefault(row["arm"], []).append(row)

    blocks: list[str] = []
    for case_id in sorted(by_case):
        per_arm = {arm: aggregate_case(reps) for arm, reps in by_case[case_id].items()}
        blocks.append(render_case_table(case_id, per_arm))
    return "\n".join(blocks) if blocks else "_No result rows found._\n"


def run_render(args: argparse.Namespace) -> int:
    """Print the regenerated §5.2 tables so the driver can paste them in.

    Keeping generation in one place means the readout's tables and the committed
    raw JSONL can never disagree (plan §4.2).
    """
    results_dir = Path(args.results_dir)
    print(render_readout_tables(results_dir))
    return 0


# --- IO helpers --------------------------------------------------------------


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


# --- CLI ---------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenCode-vs-Claude parity capture driver")
    parser.add_argument("--suite", help="path to the eval suite JSON")
    parser.add_argument("--target", default="http://localhost:18080", help="runner base URL")
    parser.add_argument("--arm", help="arm label (A|B|C)")
    parser.add_argument("--rep", type=int, help="repetition number")
    parser.add_argument("--model", help="model id under test (for the record rows)")
    parser.add_argument("--provider", help="provider label (anthropic|openrouter)")
    parser.add_argument("--otlp-port", type=int, default=4318, help="embedded OTLP port")
    parser.add_argument("--out", help="output path (JSONL results or probes)")
    parser.add_argument("--prices", help="path to the {input,output} price-table JSON")
    parser.add_argument("--probe", choices=["steer", "budget"], help="run a behavioral probe")
    parser.add_argument("--coldstart", action="store_true", help="measure /healthz readiness")
    parser.add_argument(
        "--coldstart-timeout-s", type=float, default=120.0, help="coldstart poll deadline"
    )
    parser.add_argument(
        "--start-ts",
        type=float,
        default=None,
        help="epoch seconds captured before `docker run`; makes coldstart measure "
        "true container-start-to-ready instead of poll-only",
    )
    parser.add_argument("--render", action="store_true", help="regenerate readout tables")
    parser.add_argument(
        "--results-dir",
        default="docs/evals/opencode-parity/results",
        help="results dir for --render",
    )
    return parser


async def main() -> int:
    args = build_parser().parse_args()
    if args.render:
        return run_render(args)
    if args.coldstart:
        return await run_coldstart(args)
    if args.probe:
        return await run_probe(args)
    return await run_suite(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
