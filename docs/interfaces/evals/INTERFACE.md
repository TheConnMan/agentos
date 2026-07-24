---
seam: Evals (case + scorer)
kind: SOFT
impls: 2 scorers (grader family + trajectory matcher)
grade: B
vision_row: Evals
epics:
  - "#8"
  - "#26"
order: 8
---
# INTERFACE: Evals (case + scorer)

> Part of the Curie swappable-seam catalog — see the [seam index](../../interfaces.md).
<!-- BEGIN GENERATED: header (curie dev docs-lint) -->
> **Kind:** SOFT &nbsp;·&nbsp; **Implementations today:** 2 scorers (grader family + trajectory matcher) &nbsp;·&nbsp; **Swap-readiness grade:** B
<!-- END GENERATED: header -->

**Kind legend:** CLEAN = a real `Protocol`/typed port class · SOFT = swap via env/URL/prefix/wire, no code interface · NONE = not built yet.

The scorer slot is now a typed `Scorer` Protocol above the port; the seam overall is still SOFT because the request-in / results-out contracts are wire shapes, not a code interface.

## The black line

The eval seam is defined by two wire contracts, not a code interface: the `curie:evals` Valkey stream schema (request in) and the `EvalMatrix` DTO the API reads back (results out). The scorer/grader slots in *above* the port (a case names a grader; the runner applies it), and the result store (Langfuse) sits *behind* the recorder. Swapping either the grader family or the store means honoring the same stream payload and the same version/suite-tagged trace+score shape; the stream, the recorder's tag convention, and the matrix DTO are the opinionated core.

## Current contract

Request side — one stream field `payload` (`STREAM_PAYLOAD_FIELD`, `packages/aci-protocol/src/aci_protocol/service_config.py::STREAM_PAYLOAD_FIELD`) holding an `EvalJob` JSON, the shared wire model between the API producer and the worker consumer (`packages/aci-protocol/src/aci_protocol/wire.py::EvalJob`, issue #492): `agent_id`, `version_id`, `sha`, `suite`, `bundle_ref`, `target_url`, `requested_at`. Consumer group `curie-eval-workers` is created and read by the eval stream consumer (`apps/worker/src/curie_worker/eval/stream.py::EvalStreamConsumer.ensure_group`, `apps/worker/src/curie_worker/eval/stream.py::EvalStreamConsumer._read_loop`).

Case + grader models — `EvalCase` is `{id, input, grader, shared_history, expect_status}` (`apps/worker/src/curie_worker/eval/models.py::EvalCase`), where `shared_history` (default `false`) opts a case out of the per-case fresh-conversation reset and `expect_status` (frozen `ExpectedStatus`, `apps/worker/src/curie_worker/eval/models.py::ExpectedStatus`, default `done`) asserts the turn's terminal session status, so an approval-gated case can assert `awaiting-approval` — the gate holding is a green result; `Grader` is `{kind: GraderKind, expected, case_sensitive}` (`apps/worker/src/curie_worker/eval/models.py::Grader`) where `GraderKind` is the enum-dispatched family `EXACT | CONTAINS | REGEX` (`apps/worker/src/curie_worker/eval/models.py::GraderKind`), applied by `Grader.grade(output)` (`apps/worker/src/curie_worker/eval/models.py::Grader.grade`). This is the single "grader family" — deny-by-default, string-shaped only.

Scorer seam — the pass/fail decision is now a swappable `Scorer` Protocol above the port (`apps/worker/src/curie_worker/eval/scorer.py::Scorer`): `score(case, output, trajectory) -> ScoreResult`. `EvalRunner` captures both the answer text and the tool-call `trajectory` (the ordered `tool` field of each `tool_note` frame) and delegates to an injected scorer, defaulting to `GraderScorer` (the frozen grader over the text — behavior-preserving). The second, tier-1 scorer is `TrajectoryScorer`: a deterministic matcher over the tool-call sequence with modes `EXACT | IN_ORDER | ANY_ORDER | PRECISION | RECALL`, configured above the port via a `case_id -> TrajectorySpec` mapping the run layer supplies (NOT a field on the frozen case — a per-case trajectory expectation would change the frozen eval-case schema, deliberately out of scope). LLM-judge and hosted-eval-API scorers are the later, costlier tiers; they conform to the same Protocol but are not built.

Result side — `LangfuseEvalRecorder.record()` (`apps/worker/src/curie_worker/eval/recorder.py::LangfuseEvalRecorder.record`) posts, per case, a trace tagged `["eval", f"version:{run.version}", f"suite:{run.suite}"]` (`apps/worker/src/curie_worker/eval/recorder.py::LangfuseEvalRecorder._trace_event`) plus an `eval_pass` numeric score `1.0/0.0` (`SCORE_NAME`, `apps/worker/src/curie_worker/eval/recorder.py::SCORE_NAME`; `_score_event`, `apps/worker/src/curie_worker/eval/recorder.py::LangfuseEvalRecorder._score_event`). The read path is `GET /matrix` returning `EvalMatrix`, querying traces by those tags (`apps/api/src/curie_api/routers/evals.py::eval_matrix`, which calls `list_traces_by_tags(["eval", f"suite:{suite}"])`).

Model dimension (issue #255, BYO-model epic #24) — additive, same convention as `version:`/`suite:`. A run carries the model id it ran under on `EvalRunResult.model`, which the recorder emits as a `model:<name>` tag plus a `metadata.model` field (and a per-case `metadata.cost_usd` when available). `build_matrix` reads them back (`_model_of`/`_cost_of`) into `EvalCell.model` and a per-model `EvalModelSummary` rollup (pass-rate + summed cost) on `EvalMatrix.model_summaries`, so a suite is sliceable by model for pass-rate/cost comparison. Runs with no resolved model (the `target_url` shortcut) record no `model:` tag and fall into the unlabelled column. **UI surfacing of `model_summaries` (a model column/toggle on the matrix grid) is not built here — the API DTO carries it; the console has yet to render it.** The recorder does not yet compute `cost_usd` from live usage (the ACI wire carries no usage block), so cost is present-but-`None` until a usage/pricing source is threaded — a deliberate follow-up.

Prompt-cache observability (issue #255) — `_GenerationSpan.record_usage` (`runner/src/curie_runner/otel.py::_GenerationSpan.record_usage`) stamps `cache_read_input_tokens`/`cache_creation_input_tokens` on the generation span, so a warm thread's cache reads surface on the trace — the signal a translating gateway silently broke caching.

## Implementations today

Two scorers through the `Scorer` seam — the three-variant deterministic grader family (`GraderScorer`, the default) and the deterministic tool-call trajectory matcher (`TrajectoryScorer`, five modes) — and one store (Langfuse, via `LangfuseEvalRecorder`). Both scorers write results through the identical recorder/`EvalRunResult` path. No LLM-judge/hosted scorer and no second store adapter exists yet.

## Known leakage

One seam still bleeds through. The eval-case format is now converged and frozen (issue #8, ADR-0019): both the CLI scaffold/loader (`cli/src/evals.rs`) and the worker's `load_suite_from_bundle` (`apps/worker/src/curie_worker/eval/stream.py::load_suite_from_bundle`) build to one schema, `apps/worker/schema/eval-cases.schema.json`, generated from the Pydantic `EvalSuite`/`EvalCase`/`Grader` models (`apps/worker/src/curie_worker/eval/models.py`) and guarded by the same regenerate-and-diff drift gate as the frozen packages. The CLI hand-mirrors the schema in Rust and is kept honest by a byte-level conformance test against the shared committed fixture. Still open: the `version:`/`suite:` trace-tag convention is an unfrozen string contract hand-aligned between the recorder (`apps/worker/src/curie_worker/eval/recorder.py::LangfuseEvalRecorder._trace_event`) and the matrix reader (`apps/api/src/curie_api/routers/evals.py::eval_matrix`) — a rename on one side silently breaks the grid.

## Cross-links

- **Epic(s):** #8 — converge and freeze the duplicated `cases.json` case format into one schema (landed via ADR-0019); #26 — scorer swappability (grader family beyond the deterministic three)
- **Vision doc:** [architecture-vision.md](../../architecture-vision.md) — Job 3 (Evals), grade B
- **ADR(s):** [ADR-0004](../../adr/0004-langfuse-observability-and-eval-backbone.md) — Langfuse as the single observability + eval backbone; [ADR-0019](../../adr/0019-freeze-eval-case-format.md) — converge and freeze the eval-case format
