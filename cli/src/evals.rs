//! `agentos eval`: run the bundle's eval cases through the local runner.
//!
//! Cases live in `evals/cases.json` (seeded by `agentos init`): an array of
//! `{name, input, expect_contains}`. Each case round-trips an `eval_case` ACI
//! event and the reply transcript must contain every expected substring
//! (case-insensitive). This is the CLI-local seed of the K1 eval machinery,
//! not a replacement for it.

use std::path::Path;

use agentos_aci_protocol::OutboundEvent;
use anyhow::{bail, Context, Result};
use serde::Deserialize;

#[derive(Debug, Clone, Deserialize)]
pub struct EvalCase {
    pub name: String,
    pub input: String,
    #[serde(default)]
    pub expect_contains: Vec<String>,
}

pub fn load_cases(path: &Path) -> Result<Vec<EvalCase>> {
    let body =
        std::fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
    let cases: Vec<EvalCase> = serde_json::from_str(&body)
        .with_context(|| format!("{} is not a valid eval case array", path.display()))?;
    if cases.is_empty() {
        bail!("{} contains no eval cases", path.display());
    }
    Ok(cases)
}

/// The reply transcript of a turn: streamed text plus the final text.
pub fn transcript(events: &[OutboundEvent]) -> String {
    let mut out = String::new();
    for event in events {
        match event {
            OutboundEvent::TextDelta { text, .. } => {
                out.push_str(text);
                out.push('\n');
            }
            OutboundEvent::Final { text, .. } => {
                out.push_str(text);
                out.push('\n');
            }
            _ => {}
        }
    }
    out
}

/// A case passes when the transcript contains every expected substring.
pub fn case_passes(case: &EvalCase, transcript: &str) -> bool {
    let haystack = transcript.to_lowercase();
    case.expect_contains
        .iter()
        .all(|needle| haystack.contains(&needle.to_lowercase()))
}

/// Full pass condition for a turn: it must end in a `final` with status
/// `done` AND the transcript must match. A classified-failure or interrupted
/// turn never passes, even if its error text happens to contain the expected
/// substrings.
pub fn turn_passes(case: &EvalCase, events: &[OutboundEvent]) -> bool {
    let completed = matches!(
        events.last(),
        Some(OutboundEvent::Final {
            status: agentos_aci_protocol::SessionStatus::Done,
            ..
        })
    );
    completed && case_passes(case, &transcript(events))
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
    use agentos_aci_protocol::{SessionStatus, PROTOCOL_VERSION};

    fn case(expect: &[&str]) -> EvalCase {
        EvalCase {
            name: "c".into(),
            input: "hi".into(),
            expect_contains: expect.iter().map(|s| s.to_string()).collect(),
        }
    }

    fn events() -> Vec<OutboundEvent> {
        vec![
            OutboundEvent::TextDelta {
                version: PROTOCOL_VERSION.into(),
                text: "Looking into it".into(),
            },
            OutboundEvent::ToolNote {
                version: PROTOCOL_VERSION.into(),
                text: "Bash".into(),
                tool: Some("Bash".into()),
            },
            OutboundEvent::Final {
                version: PROTOCOL_VERSION.into(),
                text: "all done".into(),
                status: SessionStatus::Done,
            },
        ]
    }

    #[test]
    fn transcript_collects_text_deltas_and_final_only() {
        assert_eq!(transcript(&events()), "Looking into it\nall done\n");
    }

    #[test]
    fn matching_is_case_insensitive_and_requires_every_needle() {
        let t = transcript(&events());
        assert!(case_passes(&case(&["ALL DONE", "looking"]), &t));
        assert!(!case_passes(&case(&["all done", "missing"]), &t));
        assert!(case_passes(&case(&[]), &t));
    }

    #[test]
    fn a_classified_failure_never_passes_even_when_text_matches() {
        let mut failed = events();
        failed.pop();
        failed.push(OutboundEvent::Final {
            version: PROTOCOL_VERSION.into(),
            text: "all done".into(),
            status: SessionStatus::ClassifiedFailure,
        });
        assert!(turn_passes(&case(&["all done"]), &events()));
        assert!(!turn_passes(&case(&["all done"]), &failed));
        // An empty expectation list is a smoke case: completion is the assert.
        assert!(turn_passes(&case(&[]), &events()));
        assert!(!turn_passes(&case(&[]), &failed));
    }

    #[test]
    fn renders_design_canon_lines() {
        assert_eq!(case_line("approver", true, 1.24), "\u{2713} approver  1.2s");
        assert_eq!(case_line("crm", false, 0.9), "\u{2717} crm  0.9s");
        assert_eq!(summary_line(34, 36), "34/36 passed");
    }

    #[test]
    fn loads_cases_and_rejects_an_empty_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("cases.json");
        std::fs::write(
            &path,
            r#"[{"name":"a","input":"b","expect_contains":["c"]}]"#,
        )
        .unwrap();
        let cases = load_cases(&path).unwrap();
        assert_eq!(cases.len(), 1);
        assert_eq!(cases[0].expect_contains, vec!["c"]);

        std::fs::write(&path, "[]").unwrap();
        assert!(load_cases(&path).is_err());
    }
}
