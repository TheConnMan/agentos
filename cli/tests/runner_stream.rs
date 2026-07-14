//! Integration: the runner client against a wire-faithful NDJSON stream.

mod support;

use std::process::Command;

use agentos::runner::RunnerClient;
use agentos_aci_protocol::{EventType, OutboundEvent, SessionStatus, PROTOCOL_VERSION};
use support::{serve, Response};

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_agentos")
}

fn frame(json: serde_json::Value) -> String {
    serde_json::to_string(&json).unwrap()
}

fn happy_turn() -> Vec<String> {
    vec![
        frame(serde_json::json!({
            "type": "text_delta", "version": PROTOCOL_VERSION, "text": "Looking into it"
        })),
        frame(serde_json::json!({
            "type": "tool_note", "version": PROTOCOL_VERSION, "text": "echo hi", "tool": "Bash"
        })),
        frame(serde_json::json!({
            "type": "side_effect_flag", "version": PROTOCOL_VERSION, "tool": "Bash", "detail": null
        })),
        frame(serde_json::json!({
            "type": "final", "version": PROTOCOL_VERSION, "text": "all done", "status": "done"
        })),
    ]
}

fn trajectory_turn(tools: &[(&str, &str)], final_text: &str) -> Vec<String> {
    trajectory_turn_with_status(tools, final_text, "done")
}

fn trajectory_turn_with_status(
    tools: &[(&str, &str)],
    final_text: &str,
    status: &str,
) -> Vec<String> {
    let mut frames: Vec<String> = tools
        .iter()
        .map(|(tool, text)| {
            frame(serde_json::json!({
                "type": "tool_note",
                "version": PROTOCOL_VERSION,
                "text": text,
                "tool": tool,
            }))
        })
        .collect();
    frames.push(frame(serde_json::json!({
        "type": "final",
        "version": PROTOCOL_VERSION,
        "text": final_text,
        "status": status,
    })));
    frames
}

fn write_trajectory_case(
    case_id: &str,
    grader_expected: &str,
    expected_tools: &[&str],
) -> (tempfile::TempDir, std::path::PathBuf) {
    let dir = tempfile::tempdir().unwrap();
    let evals = dir.path().join("evals");
    std::fs::create_dir(&evals).unwrap();
    let cases = evals.join("cases.json");
    std::fs::write(
        &cases,
        serde_json::to_vec(&serde_json::json!({
            "name": "trajectory_parity",
            "cases": [{
                "id": case_id,
                "input": "use the tools",
                "grader": {
                    "kind": "contains",
                    "expected": grader_expected,
                    "case_sensitive": false,
                },
            }],
        }))
        .unwrap(),
    )
    .unwrap();
    std::fs::write(
        evals.join("trajectory.json"),
        serde_json::to_vec(&serde_json::json!({
            (case_id): {
                "expected": expected_tools,
                "mode": "exact",
            },
        }))
        .unwrap(),
    )
    .unwrap();
    (dir, cases)
}

fn run_skill_eval(cases: &std::path::Path, runner_url: &str) -> std::process::Output {
    Command::new(bin())
        .args(["--json", "skill", "eval", "--cases"])
        .arg(cases)
        .args(["--url", runner_url])
        .output()
        .expect("run trajectory skill eval")
}

fn eval_payload(output: &std::process::Output) -> serde_json::Value {
    serde_json::from_slice(&output.stdout).unwrap_or_else(|err| {
        panic!(
            "eval stdout was not JSON: {err}; stdout={}; stderr={}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        )
    })
}

#[tokio::test]
async fn streams_a_full_turn_in_order() {
    let server = serve(|_req| Response::ndjson(&happy_turn()));
    let client = RunnerClient::new(&server.base_url).unwrap();

    let mut seen = Vec::new();
    let events = client
        .send_event(EventType::Message, "hi there", "U-test", |e| {
            seen.push(e.clone());
        })
        .await
        .unwrap();

    assert_eq!(events.len(), 4);
    assert_eq!(events, seen, "callback order must match returned order");
    assert!(matches!(
        events.first(),
        Some(OutboundEvent::TextDelta { text, .. }) if text == "Looking into it"
    ));
    assert!(matches!(
        events.last(),
        Some(OutboundEvent::Final {
            status: SessionStatus::Done,
            ..
        })
    ));

    // The inbound frame on the wire is the frozen contract's event shape.
    let recorded = server.recorded();
    let body: serde_json::Value = serde_json::from_slice(&recorded[0].body).unwrap();
    assert_eq!(body["kind"], "event");
    assert_eq!(body["type"], "message");
    assert_eq!(body["text"], "hi there");
    assert_eq!(body["user"], "U-test");
    assert!(body["ts"].as_str().unwrap().contains('.'));
}

#[tokio::test]
async fn rejects_an_off_version_stream() {
    let server = serve(|_req| {
        Response::ndjson(&[frame(serde_json::json!({
            "type": "final", "version": "9.9.9", "text": "x", "status": "done"
        }))])
    });
    let client = RunnerClient::new(&server.base_url).unwrap();
    let err = client
        .send_event(EventType::Message, "hi", "U", |_| {})
        .await
        .unwrap_err();
    assert!(err.to_string().contains("invalid ACI outbound frame"));
}

#[tokio::test]
async fn errors_when_the_stream_ends_without_a_final() {
    let server = serve(|_req| {
        Response::ndjson(&[frame(serde_json::json!({
            "type": "text_delta", "version": PROTOCOL_VERSION, "text": "partial"
        }))])
    });
    let client = RunnerClient::new(&server.base_url).unwrap();
    let err = client
        .send_event(EventType::Message, "hi", "U", |_| {})
        .await
        .unwrap_err();
    assert!(err.to_string().contains("without a final frame"));
}

#[tokio::test]
async fn surfaces_http_errors_with_the_body() {
    let server = serve(|_req| Response::json(400, r#"{"error":"invalid event frame"}"#));
    let client = RunnerClient::new(&server.base_url).unwrap();
    let err = client
        .send_event(EventType::Message, "hi", "U", |_| {})
        .await
        .unwrap_err();
    let text = err.to_string();
    assert!(text.contains("400"), "unexpected error: {text}");
    assert!(
        text.contains("invalid event frame"),
        "unexpected error: {text}"
    );
}

#[tokio::test]
async fn status_round_trips() {
    let server = serve(|req| {
        assert_eq!(req.path, "/status");
        Response::json(200, r#"{"status":"done","ready":true,"turn_active":false}"#)
    });
    let client = RunnerClient::new(&server.base_url).unwrap();
    let status = client.status().await.unwrap();
    assert_eq!(status["status"], "done");
    assert_eq!(status["ready"], true);
}

#[test]
fn skill_eval_fails_a_text_match_when_the_tool_trajectory_is_wrong() {
    let server = serve(|req| {
        assert_eq!(req.path, "/v1/event");
        Response::ndjson(&trajectory_turn(
            &[("Bash", "Read then Bash"), ("Read", "Bash then Read")],
            "all done",
        ))
    });
    let (_dir, cases) = write_trajectory_case("wrong_order", "all done", &["Read", "Bash"]);

    let output = run_skill_eval(&cases, &server.base_url);

    assert_eq!(output.status.code(), Some(1));
    let payload = eval_payload(&output);
    assert_eq!(payload["passed"], 0);
    assert_eq!(payload["failed"], 1);
    assert_eq!(payload["cases"][0]["id"], "wrong_order");
    assert_eq!(payload["cases"][0]["passed"], false);
}

#[test]
fn skill_eval_passes_from_tool_note_fields_in_the_observed_order() {
    let server = serve(|req| {
        assert_eq!(req.path, "/v1/event");
        Response::ndjson(&trajectory_turn(
            &[
                ("Read", "the prose says Bash first"),
                ("Bash", "the prose says Read second"),
            ],
            "text grader deliberately misses",
        ))
    });
    let (_dir, cases) = write_trajectory_case(
        "ordered_tools",
        "text path must not decide this",
        &["Read", "Bash"],
    );

    let output = run_skill_eval(&cases, &server.base_url);

    assert!(
        output.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let payload = eval_payload(&output);
    assert_eq!(payload["passed"], 1);
    assert_eq!(payload["failed"], 0);
    assert_eq!(payload["cases"][0]["id"], "ordered_tools");
    assert_eq!(payload["cases"][0]["passed"], true);
}

#[test]
fn skill_eval_keeps_the_completed_turn_gate_for_a_matching_trajectory() {
    let server = serve(|req| {
        assert_eq!(req.path, "/v1/event");
        Response::ndjson(&trajectory_turn_with_status(
            &[("Read", "read")],
            "all done",
            "classified-failure",
        ))
    });
    let (_dir, cases) = write_trajectory_case("failed_turn", "all done", &["Read"]);

    let output = run_skill_eval(&cases, &server.base_url);

    assert_eq!(output.status.code(), Some(1));
    let payload = eval_payload(&output);
    assert_eq!(payload["cases"][0]["id"], "failed_turn");
    assert_eq!(payload["cases"][0]["passed"], false);
}

#[test]
fn skill_eval_fails_closed_when_a_trajectory_case_has_no_spec() {
    let server = serve(|req| {
        assert_eq!(req.path, "/v1/event");
        Response::ndjson(&trajectory_turn(&[("Read", "read")], "all done"))
    });
    let (dir, cases) = write_trajectory_case("specified", "all done", &["Read"]);
    std::fs::write(
        dir.path().join("evals/cases.json"),
        r#"{"name":"trajectory_parity","cases":[{"id":"missing","input":"use the tools","grader":{"kind":"contains","expected":"all done"}}]}"#,
    )
    .unwrap();

    let output = run_skill_eval(&cases, &server.base_url);

    assert_eq!(output.status.code(), Some(1));
    let payload = eval_payload(&output);
    assert_eq!(payload["cases"][0]["id"], "missing");
    assert_eq!(payload["cases"][0]["passed"], false);
}

#[test]
fn skill_eval_rejects_duplicate_case_ids_before_runner_execution() {
    let server = serve(|req| {
        panic!(
            "duplicate case ids must fail before runner request {} {}",
            req.method, req.path
        )
    });
    let (dir, cases) = write_trajectory_case("duplicate", "all done", &["Read"]);
    std::fs::write(
        dir.path().join("evals/cases.json"),
        r#"{"name":"trajectory_parity","cases":[{"id":"duplicate","input":"first","grader":{"kind":"contains","expected":"all done"}},{"id":"duplicate","input":"second","grader":{"kind":"contains","expected":"all done"}}]}"#,
    )
    .unwrap();

    let output = run_skill_eval(&cases, &server.base_url);

    assert_eq!(output.status.code(), Some(2));
    let payload: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert!(
        payload["error"].as_str().unwrap().contains("duplicate"),
        "payload: {payload}"
    );
    assert!(server.recorded().is_empty());
}

#[test]
fn ordinary_grader_suite_allows_duplicate_case_ids_without_trajectory_selection() {
    let server = serve(|req| {
        assert_eq!(req.path, "/v1/event");
        Response::ndjson(&trajectory_turn(&[], "all done"))
    });
    let dir = tempfile::tempdir().unwrap();
    let cases = dir.path().join("cases.json");
    std::fs::write(
        &cases,
        r#"{"name":"grader_parity","cases":[{"id":"duplicate","input":"first","grader":{"kind":"contains","expected":"all done"}},{"id":"duplicate","input":"second","grader":{"kind":"contains","expected":"all done"}}]}"#,
    )
    .unwrap();

    let output = run_skill_eval(&cases, &server.base_url);

    assert!(
        output.status.success(),
        "stdout: {}; stderr: {}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let payload = eval_payload(&output);
    assert_eq!(payload["passed"], 2);
    assert_eq!(payload["failed"], 0);
    assert_eq!(server.recorded().len(), 2);
}
