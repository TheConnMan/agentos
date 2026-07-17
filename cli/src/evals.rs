//! `agentos skill eval`: run the bundle's eval cases through the local runner.
//!
//! Cases live in `evals/cases.json` (seeded by `agentos init`): a suite OBJECT
//! `{name, cases: [{id, input, grader}]}`, where each grader is one of
//! `kind: exact | contains | regex` with an `expected` string and an optional
//! `case_sensitive` flag. This shape hand-mirrors the frozen canonical eval-case
//! format owned by the worker (`apps/worker/schema/eval-cases.schema.json`, the
//! Pydantic `EvalSuite`); a shape change lands in the same reviewed change as the
//! Python models. Grading semantics mirror the platform's `Grader.grade`. This is
//! the CLI-local seed of the K1 eval machinery, not a replacement for it.

use std::path::Path;

use agentos_aci_protocol::{OutboundEvent, SessionStatus};
use anyhow::{anyhow, bail, Context, Result};
use regex::RegexBuilder;
use serde::{Deserialize, Serialize};

/// How a case's expected value is compared against the agent's answer.
// `Serialize` is derived alongside `Deserialize` so the spec scaffold path
// (`spec.rs`) can re-emit an assembled suite into `evals/cases.json`; the
// `rename_all = "lowercase"` round-trips both ways so the written kind is the
// same lowercase token `load_suite` reads back.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum GraderKind {
    Exact,
    Contains,
    Regex,
}

/// The terminal session status an eval case asserts. Mirrors the frozen
/// `ExpectedStatus` (apps/worker/.../models.py): `done` = the turn completed and
/// answered, `awaiting-approval` = an approval gate correctly held (ADR-0010).
/// A deliberate subset of `SessionStatus`; classified-failure is never an
/// expectable success. Default `done` keeps every pre-existing case unchanged.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum ExpectedStatus {
    #[default]
    Done,
    AwaitingApproval,
}

impl ExpectedStatus {
    /// True if an observed final `status` satisfies this expectation.
    pub fn matches(self, status: &SessionStatus) -> bool {
        matches!(
            (self, status),
            (ExpectedStatus::Done, SessionStatus::Done)
                | (
                    ExpectedStatus::AwaitingApproval,
                    SessionStatus::AwaitingApproval
                )
        )
    }
}

/// A single deterministic grader mirroring the worker's `Grader`.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Grader {
    pub kind: GraderKind,
    pub expected: String,
    #[serde(default)]
    pub case_sensitive: bool,
}

impl Grader {
    /// True if `output` satisfies this grader. Mirrors the platform's
    /// `Grader.grade`: exact compares whitespace-trimmed values, contains is a
    /// substring test, regex is a search; all case-fold both sides unless
    /// `case_sensitive` (regex uses the engine's case-insensitive flag).
    pub fn grade(&self, output: &str) -> bool {
        if self.kind == GraderKind::Regex {
            return match RegexBuilder::new(&self.expected)
                .case_insensitive(!self.case_sensitive)
                .build()
            {
                Ok(re) => re.is_match(output),
                Err(_) => false,
            };
        }
        // Exact and Contains fold both sides unless case_sensitive, then differ
        // only in the comparison; fold once here rather than per arm.
        let (actual, expected) = if self.case_sensitive {
            (output.to_string(), self.expected.clone())
        } else {
            (output.to_lowercase(), self.expected.to_lowercase())
        };
        if self.kind == GraderKind::Exact {
            actual.trim() == expected.trim()
        } else {
            actual.contains(&expected)
        }
    }
}

/// One eval: an input prompt and the grader that judges the answer.
/// `expect_status` asserts the turn's terminal status: default `done`, or
/// `awaiting-approval` to assert an approval gate blocked the action.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct EvalCase {
    pub id: String,
    pub input: String,
    pub grader: Grader,
    /// Per-case isolation opt-out (#550). Each case runs in a *fresh
    /// conversation* by default (`false`): `agentos skill eval` resets the runner
    /// before the case so it cannot answer from an earlier case's history instead
    /// of actually invoking its tools -- a false green for a side-effecting agent,
    /// and a silent order-dependence in the suite. Set `true` to deliberately
    /// chain a case onto the prior case's conversation (a multi-turn scenario as
    /// ordered cases); the driver then skips the reset. On the first case it is a
    /// no-op-with-caveat (no prior case to chain onto -- it only means "do not
    /// reset first", inheriting any state the runner already held). Optional with
    /// a `false`
    /// default so it stays byte-compatible with the frozen eval-case schema
    /// (`shared_history: false` is omitted on serialize, mirroring an authored
    /// suite that never wrote the field).
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub shared_history: bool,
    #[serde(default)]
    pub expect_status: ExpectedStatus,
}

/// A named set of eval cases run together against one plugin version.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct EvalSuite {
    pub name: String,
    pub cases: Vec<EvalCase>,
}

/// Validate an assembled suite: reject an empty case list and eagerly compile
/// every regex grader so a bad pattern fails now, not mid-run. Factored out of
/// `load_suite` so the spec scaffold path (`spec.rs`) enforces the identical
/// eval-case discipline against a suite it built in memory rather than read from
/// disk -- one rule, two entry points, no drift.
pub fn validate_suite(name: &str, cases: &[EvalCase]) -> Result<()> {
    if cases.is_empty() {
        bail!("suite {:?} contains no eval cases", name);
    }
    for case in cases {
        if case.grader.kind == GraderKind::Regex {
            RegexBuilder::new(&case.grader.expected)
                .build()
                .map_err(|err| {
                    anyhow!(
                        "case {:?} has an invalid regex grader {:?}: {err}. The local CLI compiles \
                         patterns with the Rust `regex` crate, a portable subset with no lookaround \
                         or backreferences; the pattern may still be valid on the platform.",
                        case.id,
                        case.grader.expected
                    )
                })?;
        }
    }
    Ok(())
}

/// Parse the suite object at `path`. Rejects an empty `cases` list, eagerly
/// compiles every regex grader (so a bad pattern fails at load, not mid-run),
/// and turns the retired top-level-array format into a targeted migration hint.
pub fn load_suite(path: &Path) -> Result<EvalSuite> {
    let body =
        std::fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
    let value: serde_json::Value = serde_json::from_str(&body)
        .with_context(|| format!("{} is not valid JSON", path.display()))?;
    if value.is_array() {
        bail!(
            "{} is in the retired eval-case format (a top-level array of \
             [{{name, input, expect_contains}}]). The eval-case format is now a suite \
             object: {{\"name\": \"...\", \"cases\": [{{\"id\": \"...\", \"input\": \"...\", \
             \"grader\": {{\"kind\": \"contains\", \"expected\": \"...\", \"case_sensitive\": false}}}}]}}. \
             Rewrite the file to the object form.",
            path.display()
        );
    }
    let suite: EvalSuite = serde_json::from_value(value)
        .with_context(|| format!("{} is not a valid eval suite", path.display()))?;
    validate_suite(&suite.name, &suite.cases)?;
    Ok(suite)
}

/// The graded answer for a turn: the `final` frame's text when a final arrived,
/// else the concatenation of the streamed text deltas. Mirrors the platform
/// runner: streamed interim text is not graded once a final exists.
pub fn graded_answer(events: &[OutboundEvent]) -> String {
    let mut final_text: Option<&str> = None;
    let mut deltas = String::new();
    for event in events {
        match event {
            OutboundEvent::Final { text, .. } => final_text = Some(text),
            OutboundEvent::TextDelta { text, .. } => deltas.push_str(text),
            _ => {}
        }
    }
    match final_text {
        Some(text) => text.to_string(),
        None => deltas,
    }
}

/// Full pass condition for a turn: it must end in a `final` whose status equals
/// the case's `expect_status` (default `done`, or `awaiting-approval` for a
/// gate-blocked case) AND the grader must match the graded answer. A
/// classified-failure or interrupted turn still never passes, because those
/// statuses match neither `done` nor `awaiting-approval`.
pub fn turn_passes(case: &EvalCase, events: &[OutboundEvent]) -> bool {
    let Some(OutboundEvent::Final { status, .. }) = events.last() else {
        return false;
    };
    case.expect_status.matches(status) && case.grader.grade(&graded_answer(events))
}

/// One rendered result line: check-or-cross, name, duration (design canon).
pub fn case_line(name: &str, passed: bool, seconds: f64) -> String {
    let mark = if passed { '\u{2713}' } else { '\u{2717}' };
    format!("{mark} {name}  {seconds:.1}s")
}

pub fn summary_line(passed: usize, total: usize) -> String {
    format!("{passed}/{total} passed")
}

#[cfg(test)]
mod tests {
    use super::*;
    use agentos_aci_protocol::PROTOCOL_VERSION;

    fn grader(kind: GraderKind, expected: &str, case_sensitive: bool) -> Grader {
        Grader {
            kind,
            expected: expected.into(),
            case_sensitive,
        }
    }

    fn case(g: Grader) -> EvalCase {
        case_with_status(g, ExpectedStatus::Done)
    }

    fn case_with_status(g: Grader, expect_status: ExpectedStatus) -> EvalCase {
        EvalCase {
            id: "c".into(),
            input: "hi".into(),
            grader: g,
            shared_history: false,
            expect_status,
        }
    }

    fn delta(text: &str) -> OutboundEvent {
        OutboundEvent::TextDelta {
            version: PROTOCOL_VERSION.into(),
            text: text.into(),
        }
    }

    fn final_event(text: &str, status: SessionStatus) -> OutboundEvent {
        OutboundEvent::Final {
            version: PROTOCOL_VERSION.into(),
            text: text.into(),
            status,
            approval_summary: None,
            approval_route: None,
            approval_gate_kind: None,
            approval_granted_tool: None,
        }
    }

    fn write(body: &str) -> (tempfile::TempDir, std::path::PathBuf) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("cases.json");
        std::fs::write(&path, body).unwrap();
        (dir, path)
    }

    #[test]
    fn loads_the_object_suite_form() {
        let (_dir, path) = write(
            r#"{"name":"s","cases":[{"id":"a","input":"b","grader":{"kind":"contains","expected":"x"}}]}"#,
        );
        let suite = load_suite(&path).unwrap();
        assert_eq!(suite.name, "s");
        assert_eq!(suite.cases.len(), 1);
        assert_eq!(suite.cases[0].id, "a");
        assert_eq!(suite.cases[0].grader.kind, GraderKind::Contains);
        assert!(!suite.cases[0].grader.case_sensitive);
        // An absent expect_status defaults to Done, keeping pre-existing cases
        // byte-identical in behavior.
        assert_eq!(suite.cases[0].expect_status, ExpectedStatus::Done);
    }

    #[test]
    fn loads_expect_status_awaiting_approval() {
        let (_dir, path) = write(
            r#"{"name":"s","cases":[{"id":"a","input":"b","grader":{"kind":"contains","expected":"x"},"expect_status":"awaiting-approval"}]}"#,
        );
        let suite = load_suite(&path).unwrap();
        assert_eq!(
            suite.cases[0].expect_status,
            ExpectedStatus::AwaitingApproval
        );
    }

    #[test]
    fn shared_history_defaults_to_false_and_reads_true_when_set() {
        // Omitted -> false (backward compatible with every authored suite).
        let (_dir, path) = write(
            r#"{"name":"s","cases":[{"id":"a","input":"b","grader":{"kind":"contains","expected":"x"}}]}"#,
        );
        assert!(!load_suite(&path).unwrap().cases[0].shared_history);
        // Present and true -> the case opts into the prior case's conversation.
        let (_dir2, path2) = write(
            r#"{"name":"s","cases":[{"id":"a","input":"b","grader":{"kind":"contains","expected":"x"},"shared_history":true}]}"#,
        );
        assert!(load_suite(&path2).unwrap().cases[0].shared_history);
    }

    #[test]
    fn a_false_shared_history_is_omitted_on_serialize() {
        // Byte-compat with the frozen schema: a fresh-conversation case (the
        // default) serializes exactly as a suite that never wrote the field, so
        // the scaffold and spec-authored cases.json stay unchanged.
        let json = serde_json::to_string(&case(grader(GraderKind::Contains, "x", false))).unwrap();
        assert!(!json.contains("shared_history"), "got: {json}");
    }

    #[test]
    fn rejects_the_retired_array_form_with_a_migration_hint() {
        let (_dir, path) = write(r#"[{"name":"a","input":"b","expect_contains":["c"]}]"#);
        let err = load_suite(&path).unwrap_err().to_string();
        assert!(err.contains("retired eval-case format"), "{err}");
        assert!(err.contains("expect_contains"), "{err}");
        assert!(err.contains("\"cases\""), "{err}");
    }

    #[test]
    fn rejects_an_empty_cases_list() {
        let (_dir, path) = write(r#"{"name":"s","cases":[]}"#);
        let err = load_suite(&path).unwrap_err().to_string();
        assert!(err.contains("no eval cases"), "{err}");
    }

    #[test]
    fn rejects_an_unknown_grader_kind() {
        let (_dir, path) = write(
            r#"{"name":"s","cases":[{"id":"a","input":"b","grader":{"kind":"llm_judge","expected":"x"}}]}"#,
        );
        assert!(load_suite(&path).is_err());
    }

    #[test]
    fn rejects_an_invalid_regex_grader_at_load() {
        let (_dir, path) = write(
            r#"{"name":"s","cases":[{"id":"a","input":"b","grader":{"kind":"regex","expected":"(unclosed"}}]}"#,
        );
        let err = load_suite(&path).unwrap_err().to_string();
        assert!(err.contains("invalid regex grader"), "{err}");
        assert!(err.contains("may still be valid on the platform"), "{err}");
    }

    #[test]
    fn exact_grader_trims_and_case_folds() {
        assert!(grader(GraderKind::Exact, "  Done  ", false).grade("done"));
        assert!(!grader(GraderKind::Exact, "done", true).grade("Done"));
        assert!(grader(GraderKind::Exact, "done", true).grade("  done  "));
        assert!(!grader(GraderKind::Exact, "done", false).grade("all done"));
    }

    #[test]
    fn contains_grader_case_folds_unless_flagged() {
        assert!(grader(GraderKind::Contains, "WEATHER", false).grade("the weather today"));
        assert!(!grader(GraderKind::Contains, "WEATHER", true).grade("the weather today"));
        assert!(grader(GraderKind::Contains, "weather", true).grade("the weather today"));
    }

    #[test]
    fn regex_grader_searches_with_optional_case_flag() {
        assert!(grader(GraderKind::Regex, "wea.her", false).grade("The WEATHER"));
        assert!(!grader(GraderKind::Regex, "WEA.HER", true).grade("the weather"));
        assert!(grader(GraderKind::Regex, "^done$", false).grade("DONE"));
    }

    #[test]
    fn graded_answer_is_final_text_when_a_final_exists() {
        let events = vec![
            delta("Looking into it"),
            final_event("all done", SessionStatus::Done),
        ];
        assert_eq!(graded_answer(&events), "all done");
    }

    #[test]
    fn graded_answer_joins_deltas_when_no_final() {
        let events = vec![delta("Looking "), delta("into it")];
        assert_eq!(graded_answer(&events), "Looking into it");
    }

    #[test]
    fn a_classified_failure_never_passes_even_when_text_matches() {
        let done = vec![
            delta("Looking into it"),
            final_event("all done", SessionStatus::Done),
        ];
        let failed = vec![
            delta("Looking into it"),
            final_event("all done", SessionStatus::ClassifiedFailure),
        ];
        let c = case(grader(GraderKind::Contains, "all done", false));
        assert!(turn_passes(&c, &done));
        assert!(!turn_passes(&c, &failed));
    }

    #[test]
    fn gate_blocked_turn_is_green_and_narrate_only_is_red() {
        // The run-7 anti-correlation, encoded: an approval-gated case that asserts
        // `awaiting-approval` with a match-anything grader is GREEN when the gate
        // holds (turn ends awaiting-approval) and RED when the agent merely
        // narrated and the turn completed (done). Before this change the pass
        // condition hardcoded Done, so "the gate correctly blocked" was RED and
        // "the agent narrated" was GREEN -- scoring anti-correlated with safety.
        let gated = case_with_status(
            grader(GraderKind::Contains, "", false),
            ExpectedStatus::AwaitingApproval,
        );
        let held = vec![final_event(
            "blocked the close",
            SessionStatus::AwaitingApproval,
        )];
        let narrated = vec![final_event("I asked for approval", SessionStatus::Done)];
        assert!(turn_passes(&gated, &held)); // the gate held -> GREEN
        assert!(!turn_passes(&gated, &narrated)); // agent just narrated -> RED

        // Inverse guard: a default (Done) case never passes on an awaiting-approval
        // final, so widening the enum did not loosen the default gate.
        let default_case = case(grader(GraderKind::Contains, "", false));
        assert!(!turn_passes(&default_case, &held));
    }

    #[test]
    fn every_schema_expected_status_deserializes() {
        // The frozen eval-case schema owns the expected-status vocabulary (#262).
        // Every value it enumerates must round-trip through the Rust loader, so a
        // value added to the schema but not to this crate's ExpectedStatus enum
        // fails here rather than silently rejecting a valid platform-authored case.
        let schema: serde_json::Value = serde_json::from_str(include_str!(
            "../../apps/worker/schema/eval-cases.schema.json"
        ))
        .expect("committed eval-cases schema is valid JSON");
        let statuses = schema["$defs"]["ExpectedStatus"]["enum"]
            .as_array()
            .expect("ExpectedStatus enum is an array");
        assert!(!statuses.is_empty(), "schema declares no expected statuses");
        for status in statuses {
            let status = status.as_str().expect("expected status is a string");
            let body = format!(
                r#"{{"name":"s","cases":[{{"id":"c","input":"i","grader":{{"kind":"contains","expected":"x"}},"expect_status":"{status}"}}]}}"#
            );
            let (_dir, path) = write(&body);
            let suite = load_suite(&path).unwrap_or_else(|e| {
                panic!("schema expected status {status:?} was rejected by the Rust loader: {e}")
            });
            assert_eq!(suite.cases.len(), 1);
        }
    }

    #[test]
    fn loads_the_committed_weather_example() {
        // The exact bytes `agentos skill eval` reads on `examples/weather`.
        let body = include_str!("../../examples/weather/evals/cases.json");
        let (_dir, path) = write(body);
        let suite = load_suite(&path).unwrap();
        assert_eq!(suite.cases.len(), 1);
        let case = &suite.cases[0];
        // Falsifiable case (#527): a real answer must carry a temperature figure,
        // and the loader ignores the documentation-only `note` key on the case.
        assert_eq!(case.id, "reports-a-temperature");
        assert_eq!(case.grader.kind, GraderKind::Regex);
        // #620: the pattern accepts the degree glyph AND the spelled-out unit, so
        // a correct plain-English forecast is no longer graded red. The alternation
        // stays inside the Python-re / Rust-regex intersection (no lookaround, no
        // backreferences), so the CLI compiles it identically to the platform.
        assert_eq!(case.grader.expected, "\\d+\\s*(°|deg)");
    }

    #[test]
    fn weather_grader_accepts_glyph_and_spelled_unit_but_not_a_figureless_refusal() {
        // #620: prove the committed pattern's behavior by EXECUTING the grader
        // (not by inspecting the string) against the acceptance strings. The glyph
        // and both spellings pass; a refusal that carries no figure fails.
        let body = include_str!("../../examples/weather/evals/cases.json");
        let (_dir, path) = write(body);
        let grader = &load_suite(&path).unwrap().cases[0].grader;
        assert!(grader.grade("68°"), "glyph form must pass");
        assert!(grader.grade("68 deg F"), "abbreviated unit must pass");
        assert!(
            grader.grade("The high in San Francisco today is 68 degrees Fahrenheit"),
            "spelled-out unit must pass"
        );
        assert!(
            !grader.grade("I could not confirm a current forecast"),
            "a refusal with no temperature figure must still fail"
        );
    }

    #[test]
    fn every_schema_grader_kind_deserializes() {
        // The frozen eval-case schema owns the grader-kind vocabulary (#500).
        // Every kind it enumerates must round-trip through the Rust loader, so a
        // kind added to the schema but not to this crate's GraderKind enum fails
        // here rather than silently rejecting a valid platform-authored case.
        let schema: serde_json::Value = serde_json::from_str(include_str!(
            "../../apps/worker/schema/eval-cases.schema.json"
        ))
        .expect("committed eval-cases schema is valid JSON");
        let kinds = schema["$defs"]["GraderKind"]["enum"]
            .as_array()
            .expect("GraderKind enum is an array");
        assert!(!kinds.is_empty(), "schema declares no grader kinds");
        for kind in kinds {
            let kind = kind.as_str().expect("grader kind is a string");
            let body = format!(
                r#"{{"name":"s","cases":[{{"id":"c","input":"i","grader":{{"kind":"{kind}","expected":"x"}}}}]}}"#
            );
            let (_dir, path) = write(&body);
            let suite = load_suite(&path).unwrap_or_else(|e| {
                panic!("schema grader kind {kind:?} was rejected by the Rust loader: {e}")
            });
            assert_eq!(suite.cases.len(), 1);
        }
    }

    #[test]
    fn renders_design_canon_lines() {
        assert_eq!(case_line("approver", true, 1.24), "\u{2713} approver  1.2s");
        assert_eq!(case_line("crm", false, 0.9), "\u{2717} crm  0.9s");
        assert_eq!(summary_line(34, 36), "34/36 passed");
    }
}
