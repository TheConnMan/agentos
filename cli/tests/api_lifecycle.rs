//! Integration: the agent-lifecycle verbs (`cluster kill|resume|budget|delete`,
//! #149) against the committed platform-API contract shapes (apps/api
//! openapi.json), served by the wire-level test server. Covers both the
//! `ApiClient` methods (correct HTTP method + path + body) and the command
//! handlers (`--yes` gate on the destructive verbs, `--dry-run` makes no
//! request).

mod support;

use agentos::api::{ApiClient, BudgetConfig};
use agentos::commands::{self, AgentActionOpts};
use support::{serve, Response};

const AGENT_ID: &str = "11111111-1111-1111-1111-111111111111";

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
