# INTERFACE: Evals (case + scorer)

> Part of the AgentOS swappable-seam catalog — see the [seam index](../../interfaces.md).
> **Kind:** SOFT (scorer slot now a typed `Scorer` Protocol) &nbsp;·&nbsp; **Implementations today:** 2 scorers (grader family + deterministic trajectory matcher) &nbsp;·&nbsp; **Swap-readiness grade:** B

**Kind legend:** CLEAN = a real `Protocol`/typed port class · SOFT = swap via env/URL/prefix/wire, no code interface · NONE = not built yet.

## The black line

The eval seam is defined by two wire contracts, not a code interface: the `agentos:evals` Valkey stream schema (request in) and the `EvalMatrix` DTO the API reads back (results out). The scorer/grader slots in *above* the port (a case names a grader; the runner applies it), and the result store (Langfuse) sits *behind* the recorder. Swapping either the grader family or the store means honoring the same stream payload and the same version/suite-tagged trace+score shape; the stream, the recorder's tag convention, and the matrix DTO are the opinionated core.

## Current contract

Request side — one stream field `payload` (`STREAM_PAYLOAD_FIELD`, `apps/worker/src/agentos_worker/eval/stream.py:72`) holding an `EvalWorkItem` JSON (`stream.py:76`): `agent_id`, `version_id`, `sha`, `suite`, `bundle_ref`, `target_url`, `requested_at`. Consumer group `agentos-eval-workers` reads it (`stream.py:6`, `stream.py:181`).

Case + grader models — `EvalCase` is `{id, input, grader}` (`eval/models.py:53`); `Grader` is `{kind: GraderKind, expected, case_sensitive}` (`models.py:26`) where `GraderKind` is the enum-dispatched family `EXACT | CONTAINS | REGEX` (`models.py:18`), applied by `Grader.grade(output)` (`models.py:39`). This is the single "grader family" — deny-by-default, string-shaped only.

Scorer seam — the pass/fail decision is now a swappable `Scorer` Protocol above the port (`eval/scorer.py`): `score(case, output, trajectory) -> ScoreResult`. `EvalRunner` captures both the answer text and the tool-call `trajectory` (the ordered `tool` field of each `tool_note` frame) and delegates to an injected scorer, defaulting to `GraderScorer` (the frozen grader over the text — behavior-preserving). The second, tier-1 scorer is `TrajectoryScorer`: a deterministic matcher over the tool-call sequence with modes `EXACT | IN_ORDER | ANY_ORDER | PRECISION | RECALL`, configured above the port via a `case_id -> TrajectorySpec` mapping the run layer supplies (NOT a field on the frozen case — a per-case trajectory expectation would change the frozen eval-case schema, deliberately out of scope). LLM-judge and hosted-eval-API scorers are the later, costlier tiers; they conform to the same Protocol but are not built.

Result side — `LangfuseEvalRecorder.record()` (`eval/recorder.py:47`) posts, per case, a trace tagged `["eval", f"version:{run.version}", f"suite:{run.suite}"]` (`recorder.py:82`) plus an `eval_pass` numeric score `1.0/0.0` (`SCORE_NAME`, `recorder.py:25`; `_score_event`, `recorder.py:96`). The read path is `GET /matrix` returning `EvalMatrix`, querying traces by those tags (`apps/api/src/agentos_api/routers/evals.py:16`; `list_traces_by_tags(["eval", f"suite:{suite}"])` at `evals.py:23`).

## Implementations today

Two scorers through the `Scorer` seam — the three-variant deterministic grader family (`GraderScorer`, the default) and the deterministic tool-call trajectory matcher (`TrajectoryScorer`, five modes) — and one store (Langfuse, via `LangfuseEvalRecorder`). Both scorers write results through the identical recorder/`EvalRunResult` path. No LLM-judge/hosted scorer and no second store adapter exists yet.

## Known leakage

One seam still bleeds through. The eval-case format is now converged and frozen (issue #8, ADR-0019): both the CLI scaffold/loader (`cli/src/evals.rs`) and the worker's `load_suite_from_bundle` (`stream.py:143`) build to one schema, `apps/worker/schema/eval-cases.schema.json`, generated from the Pydantic `EvalSuite`/`EvalCase`/`Grader` models (`models.py`) and guarded by the same regenerate-and-diff drift gate as the frozen packages. The CLI hand-mirrors the schema in Rust and is kept honest by a byte-level conformance test against the shared committed fixture. Still open: the `version:`/`suite:` trace-tag convention is an unfrozen string contract hand-aligned between the recorder (`recorder.py:82`) and the matrix reader (`evals.py:23`) — a rename on one side silently breaks the grid.

## Cross-links

- **Epic(s):** #8 — converge and freeze the duplicated `cases.json` case format into one schema; #26 — scorer swappability (grader family beyond the deterministic three)
- **Vision doc:** [architecture-vision.md](../../architecture-vision.md) — Job 3 (Evals), grade B
- **ADR(s):** [ADR-0004](../../adr/0004-langfuse-observability-and-eval-backbone.md) — Langfuse as the single observability + eval backbone
