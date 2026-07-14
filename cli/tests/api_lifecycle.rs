//! Integration: the agent-lifecycle verbs (`cluster kill|resume|budget|delete`,
//! #149) against the committed platform-API contract shapes (apps/api
//! openapi.json), served by the wire-level test server. Covers both the
//! `ApiClient` methods (correct HTTP method + path + body) and the command
//! handlers (`--yes` gate on the destructive verbs, `--dry-run` makes no
//! request).

mod support;

use std::collections::HashMap;
use std::process::Command;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use agentos::api::{ApiClient, BudgetConfig};
use agentos::commands::{self, AgentActionOpts};
use support::{serve, MockServer, Response};

const AGENT_ID: &str = "11111111-1111-1111-1111-111111111111";
const VERSION_ID: &str = "22222222-2222-2222-2222-222222222222";
const TRAJECTORY_CASES_JSON: &str = r#"{"name":"trajectory_parity","cases":[{"id":"ordered_tools","input":"use the tools","grader":{"kind":"contains","expected":"text path must not decide this"}}]}"#;
const TRAJECTORY_CASES_SHA256: &str =
    "a947e4b9fc49b552f62b2a7e0bced9a7123cb2e0528ebcf76b57356f880385ee";

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_agentos")
}

/// A one-agent `GET /agents` list used by the handler-level resolution tests.
fn agent_list() -> Response {
    Response::json(
        200,
        &format!(
            r##"[{{"id":"{AGENT_ID}","name":"deal-desk","slack_channel":"#x","created_at":"2026-07-05T00:00:00Z"}}]"##
        ),
    )
}

fn opts(base_url: &str, agent: &str, dry_run: bool) -> AgentActionOpts {
    AgentActionOpts {
        api_url: base_url.to_string(),
        api_key: "k".to_string(),
        agent: agent.to_string(),
        dry_run,
    }
}

fn write_trajectory_suite() -> (tempfile::TempDir, std::path::PathBuf) {
    let dir = tempfile::tempdir().unwrap();
    let evals = dir.path().join("evals");
    std::fs::create_dir(&evals).unwrap();
    let cases = evals.join("cases.json");
    std::fs::write(&cases, TRAJECTORY_CASES_JSON).unwrap();
    std::fs::write(
        evals.join("trajectory.json"),
        r#"{"ordered_tools":{"expected":["Read","Bash"],"mode":"exact","threshold":1.0}}"#,
    )
    .unwrap();
    (dir, cases)
}

fn trajectory_api(
    current_status: &'static str,
) -> (MockServer, Arc<Mutex<Option<String>>>, Arc<AtomicUsize>) {
    let triggered_suite: Arc<Mutex<Option<String>>> = Arc::new(Mutex::new(None));
    let matrix_polls = Arc::new(AtomicUsize::new(0));
    let suite_for_server = Arc::clone(&triggered_suite);
    let polls_for_server = Arc::clone(&matrix_polls);
    let server = serve(move |req| match (req.method.as_str(), req.path.as_str()) {
        ("GET", "/agents") => agent_list(),
        ("POST", "/evals/trigger") => {
            assert_eq!(req.header("x-api-key"), Some("k"));
            let body: serde_json::Value = serde_json::from_slice(&req.body).unwrap();
            assert_eq!(body["agent_id"], AGENT_ID);
            assert_eq!(
                body["trajectory_specs"],
                serde_json::json!({
                    "ordered_tools": {
                        "expected": ["Read", "Bash"],
                        "mode": "exact",
                        "threshold": 1.0,
                    },
                })
            );
            assert_eq!(body["case_ids"], serde_json::json!(["ordered_tools"]));
            assert_eq!(body["cases_sha256"], TRAJECTORY_CASES_SHA256);
            let suite = body["suite"]
                .as_str()
                .expect("trajectory trigger carries a suite invocation")
                .to_string();
            assert!(suite.starts_with("trajectory_parity"), "suite: {suite}");
            assert_ne!(suite, "trajectory_parity", "the invocation must be unique");
            *suite_for_server.lock().unwrap() = Some(suite.clone());
            Response::json(
                200,
                &serde_json::json!({
                    "stream_id": "1_0",
                    "agent_id": AGENT_ID,
                    "version_id": VERSION_ID,
                    "sha": "sha_current",
                    "suite": suite,
                    "bundle_ref": "bundles/agent/version.tar.gz",
                })
                .to_string(),
            )
        }
        ("GET", path) if path.starts_with("/evals/matrix?") => {
            let query = path.split_once('?').unwrap().1;
            let params: HashMap<String, String> = serde_urlencoded::from_str(query).unwrap();
            let expected_suite = suite_for_server
                .lock()
                .unwrap()
                .clone()
                .expect("matrix polling follows the trigger");
            assert_eq!(params.get("suite"), Some(&expected_suite));
            let poll = polls_for_server.fetch_add(1, Ordering::SeqCst);
            let (sha, status) = if poll == 0 {
                ("sha_stale", "pass")
            } else {
                ("sha_current", current_status)
            };
            Response::json(
                200,
                &serde_json::json!({
                    "suite": expected_suite,
                    "versions": [sha],
                    "cases": ["ordered_tools"],
                    "rows": [{
                        "case_id": "ordered_tools",
                        "cells": [{"version": sha, "status": status, "model": null}],
                    }],
                    "models": [],
                    "model_summaries": [],
                })
                .to_string(),
            )
        }
        other => panic!("unexpected trajectory eval request: {other:?}"),
    });
    (server, triggered_suite, matrix_polls)
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

#[cfg(unix)]
fn fake_kubectl() -> tempfile::TempDir {
    use std::os::unix::fs::PermissionsExt;

    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("kubectl");
    std::fs::write(&path, "#!/bin/sh\nexec sleep 30\n").unwrap();
    let mut permissions = std::fs::metadata(&path).unwrap().permissions();
    permissions.set_mode(0o755);
    std::fs::set_permissions(path, permissions).unwrap();
    dir
}

// --- ApiClient methods: correct verb + path + body ------------------------

#[tokio::test]
async fn kill_agent_posts_to_kill_endpoint_with_empty_body() {
    let server = serve(|req| match (req.method.as_str(), req.path.as_str()) {
        ("POST", p) if *p == format!("/agents/{AGENT_ID}/kill") => {
            Response::json(200, r#"{"killed":true}"#)
        }
        other => panic!("unexpected request: {other:?}"),
    });
    let client = ApiClient::new(&server.base_url, "k").unwrap();
    let state = client.kill_agent(AGENT_ID).await.unwrap();
    assert!(state.killed);

    let rec = server.recorded();
    assert_eq!(rec.len(), 1);
    assert_eq!(rec[0].method, "POST");
    assert_eq!(rec[0].path, format!("/agents/{AGENT_ID}/kill"));
    assert!(rec[0].body.is_empty(), "kill sends no body");
    assert_eq!(rec[0].header("x-api-key"), Some("k"));
}

#[tokio::test]
async fn resume_agent_posts_to_resume_endpoint() {
    let server = serve(|req| match (req.method.as_str(), req.path.as_str()) {
        ("POST", p) if *p == format!("/agents/{AGENT_ID}/resume") => {
            Response::json(200, r#"{"killed":false}"#)
        }
        other => panic!("unexpected request: {other:?}"),
    });
    let client = ApiClient::new(&server.base_url, "k").unwrap();
    let state = client.resume_agent(AGENT_ID).await.unwrap();
    assert!(!state.killed);

    let rec = server.recorded();
    assert_eq!(rec[0].method, "POST");
    assert_eq!(rec[0].path, format!("/agents/{AGENT_ID}/resume"));
}

#[tokio::test]
async fn set_budget_puts_the_limit_as_max_usd_per_day() {
    let server = serve(|req| match (req.method.as_str(), req.path.as_str()) {
        ("PUT", p) if *p == format!("/agents/{AGENT_ID}/budget") => Response::json(
            200,
            r#"{"max_output_tokens_per_run":null,"max_usd_per_day":7.5}"#,
        ),
        other => panic!("unexpected request: {other:?}"),
    });
    let client = ApiClient::new(&server.base_url, "k").unwrap();
    let cfg = BudgetConfig {
        max_output_tokens_per_run: None,
        max_usd_per_day: Some(7.5),
    };
    let saved = client.set_budget(AGENT_ID, &cfg).await.unwrap();
    assert_eq!(saved.max_usd_per_day, Some(7.5));

    let rec = server.recorded();
    assert_eq!(rec[0].method, "PUT");
    assert_eq!(rec[0].path, format!("/agents/{AGENT_ID}/budget"));
    let body = String::from_utf8_lossy(&rec[0].body);
    assert!(body.contains("\"max_usd_per_day\":7.5"), "body: {body}");
    // The unset token cap is skipped, not sent as null, so the server keeps its
    // platform default.
    assert!(
        !body.contains("max_output_tokens_per_run"),
        "unset field must be omitted: {body}"
    );
}

#[tokio::test]
async fn delete_agent_issues_a_delete() {
    let server = serve(|req| match (req.method.as_str(), req.path.as_str()) {
        ("DELETE", p) if *p == format!("/agents/{AGENT_ID}") => Response {
            status: 204,
            content_type: "application/json".into(),
            body: Vec::new(),
        },
        other => panic!("unexpected request: {other:?}"),
    });
    let client = ApiClient::new(&server.base_url, "k").unwrap();
    client.delete_agent(AGENT_ID).await.unwrap();

    let rec = server.recorded();
    assert_eq!(rec[0].method, "DELETE");
    assert_eq!(rec[0].path, format!("/agents/{AGENT_ID}"));
    assert_eq!(rec[0].header("x-api-key"), Some("k"));
}

#[tokio::test]
async fn find_agent_errors_when_no_agent_matches() {
    let server = serve(|req| match (req.method.as_str(), req.path.as_str()) {
        ("GET", "/agents") => Response::json(200, "[]"),
        other => panic!("unexpected request: {other:?}"),
    });
    let client = ApiClient::new(&server.base_url, "k").unwrap();
    let err = client.find_agent("nope").await.unwrap_err();
    assert!(err.to_string().contains("no agent found"), "{err}");
}

// --- Handlers: resolve-then-act, --yes gate, --dry-run --------------------

#[tokio::test]
async fn kill_handler_resolves_by_name_then_kills() {
    let server = serve(|req| match (req.method.as_str(), req.path.as_str()) {
        ("GET", "/agents") => agent_list(),
        ("POST", p) if *p == format!("/agents/{AGENT_ID}/kill") => {
            Response::json(200, r#"{"killed":true}"#)
        }
        other => panic!("unexpected request: {other:?}"),
    });
    commands::kill(opts(&server.base_url, "deal-desk", false), true)
        .await
        .unwrap();

    let flow: Vec<(String, String)> = server
        .recorded()
        .iter()
        .map(|r| (r.method.clone(), r.path.clone()))
        .collect();
    assert_eq!(
        flow,
        vec![
            ("GET".to_string(), "/agents".to_string()),
            ("POST".to_string(), format!("/agents/{AGENT_ID}/kill")),
        ]
    );
}

#[tokio::test]
async fn budget_handler_resolves_then_puts_the_limit() {
    let server = serve(|req| match (req.method.as_str(), req.path.as_str()) {
        ("GET", "/agents") => agent_list(),
        ("PUT", p) if *p == format!("/agents/{AGENT_ID}/budget") => Response::json(
            200,
            r#"{"max_output_tokens_per_run":null,"max_usd_per_day":9.0}"#,
        ),
        other => panic!("unexpected request: {other:?}"),
    });
    commands::budget(opts(&server.base_url, "deal-desk", false), 9.0)
        .await
        .unwrap();

    let rec = server.recorded();
    assert_eq!(rec.len(), 2);
    assert_eq!(rec[1].method, "PUT");
    assert_eq!(rec[1].path, format!("/agents/{AGENT_ID}/budget"));
    let body = String::from_utf8_lossy(&rec[1].body);
    assert!(body.contains("\"max_usd_per_day\":9.0"), "body: {body}");
}

#[tokio::test]
async fn kill_without_yes_refuses_and_makes_no_request() {
    let server = serve(|req| panic!("no request expected, got {} {}", req.method, req.path));
    let err = commands::kill(opts(&server.base_url, "deal-desk", false), false)
        .await
        .unwrap_err();
    assert!(err.to_string().contains("--yes"), "{err}");
    assert!(
        server.recorded().is_empty(),
        "a refused kill must make no request"
    );
}

#[tokio::test]
async fn delete_without_yes_refuses_and_makes_no_request() {
    let server = serve(|req| panic!("no request expected, got {} {}", req.method, req.path));
    let err = commands::delete(opts(&server.base_url, "deal-desk", false), false)
        .await
        .unwrap_err();
    assert!(err.to_string().contains("--yes"), "{err}");
    assert!(
        server.recorded().is_empty(),
        "a refused delete must make no request"
    );
}

#[tokio::test]
async fn dry_run_makes_no_request_for_any_verb() {
    // Even a destructive verb under --dry-run (without --yes) touches nothing.
    let server = serve(|req| panic!("dry-run must not request, got {} {}", req.method, req.path));
    let base = &server.base_url;
    commands::kill(opts(base, "a", true), false).await.unwrap();
    commands::resume(opts(base, "a", true)).await.unwrap();
    commands::budget(opts(base, "a", true), 5.0).await.unwrap();
    commands::delete(opts(base, "a", true), false)
        .await
        .unwrap();
    assert!(
        server.recorded().is_empty(),
        "no dry-run verb may make a request"
    );
}

#[test]
fn local_trajectory_eval_triggers_and_polls_the_structured_worker_result() {
    let (server, triggered_suite, matrix_polls) = trajectory_api("pass");
    let (_dir, cases) = write_trajectory_suite();

    let output = Command::new(bin())
        .args(["--json", "local", "eval", "--cases"])
        .arg(&cases)
        .args([
            "--api-url",
            &server.base_url,
            "--api-key",
            "k",
            "--timeout-secs",
            "3",
        ])
        .output()
        .expect("run local trajectory eval");

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
    assert!(triggered_suite.lock().unwrap().is_some());
    assert!(matrix_polls.load(Ordering::SeqCst) >= 2);

    let paths: Vec<String> = server
        .recorded()
        .iter()
        .map(|request| request.path.clone())
        .collect();
    assert!(paths.iter().any(|path| path == "/evals/trigger"));
    assert!(paths.iter().any(|path| path.starts_with("/evals/matrix?")));
}

#[cfg(unix)]
#[test]
fn cluster_trajectory_eval_renders_failure_from_the_same_worker_result_path() {
    let (server, triggered_suite, matrix_polls) = trajectory_api("fail");
    let (_dir, cases) = write_trajectory_suite();
    let kubectl = fake_kubectl();
    let api_port = server.base_url.rsplit_once(':').unwrap().1;
    let current_path = std::env::var_os("PATH").unwrap_or_default();
    let path = std::env::join_paths(
        std::iter::once(kubectl.path().to_path_buf()).chain(std::env::split_paths(&current_path)),
    )
    .unwrap();

    let output = Command::new(bin())
        .args(["--json", "cluster", "eval", "--cases"])
        .arg(&cases)
        .args([
            "--listen-host",
            "127.0.0.1",
            "--api-local-port",
            api_port,
            "--api-key",
            "k",
            "--timeout-secs",
            "3",
        ])
        .env("PATH", path)
        .output()
        .expect("run cluster trajectory eval");

    assert_eq!(output.status.code(), Some(1));
    let payload = eval_payload(&output);
    assert_eq!(payload["passed"], 0);
    assert_eq!(payload["failed"], 1);
    assert_eq!(payload["cases"][0]["id"], "ordered_tools");
    assert_eq!(payload["cases"][0]["passed"], false);
    assert!(triggered_suite.lock().unwrap().is_some());
    assert!(matrix_polls.load(Ordering::SeqCst) >= 2);

    let paths: Vec<String> = server
        .recorded()
        .iter()
        .map(|request| request.path.clone())
        .collect();
    assert!(paths.iter().any(|path| path == "/evals/trigger"));
    assert!(paths.iter().any(|path| path.starts_with("/evals/matrix?")));
}
