//! Integration: the runner client's steer/interrupt control calls against the
//! runner's frozen HTTP contract, served by a wire-level test server.

mod support;

use agentos::runner::{RunnerClient, SteerOutcome};
use support::{serve, Response};

#[tokio::test]
async fn steer_posts_an_event_frame_and_reports_delivered() {
    let server = serve(|req| {
        assert_eq!(req.method, "POST");
        assert_eq!(req.path, "/v1/steer");
        let body: serde_json::Value = serde_json::from_slice(&req.body).unwrap();
        // The frozen InboundMessage event shape.
        assert_eq!(body["kind"], "event");
        assert_eq!(body["type"], "message");
        assert_eq!(body["text"], "follow up");
        assert_eq!(body["user"], "U-local");
        assert!(body["ts"].as_str().unwrap().contains('.'));
        Response::json(200, r#"{"ok":true}"#)
    });

    let client = RunnerClient::new(&server.base_url).unwrap();
    let outcome = client.steer("follow up", "U-local").await.unwrap();
    assert_eq!(outcome, SteerOutcome::Delivered);
    assert_eq!(server.recorded().len(), 1);
}

#[tokio::test]
async fn steer_maps_409_to_no_active_turn() {
    let server = serve(|_req| {
        Response::json(
            409,
            r#"{"error":"no active turn to steer; open a new /v1/event"}"#,
        )
    });
    let client = RunnerClient::new(&server.base_url).unwrap();
    let outcome = client.steer("follow up", "U-local").await.unwrap();
    assert_eq!(outcome, SteerOutcome::NoActiveTurn);
}

#[tokio::test]
async fn steer_surfaces_unexpected_status_as_error() {
    let server = serve(|_req| Response::json(500, r#"{"error":"boom"}"#));
    let client = RunnerClient::new(&server.base_url).unwrap();
    let err = client.steer("x", "U-local").await.unwrap_err();
    assert!(err.to_string().contains("500"), "{err}");
    assert!(err.to_string().contains("boom"), "{err}");
}

#[tokio::test]
async fn interrupt_posts_an_interrupt_frame() {
    let server = serve(|req| {
        assert_eq!(req.method, "POST");
        assert_eq!(req.path, "/v1/interrupt");
        let body: serde_json::Value = serde_json::from_slice(&req.body).unwrap();
        assert_eq!(body["kind"], "interrupt");
        assert_eq!(body["reason"], "user interrupt");
        Response::json(200, r#"{"ok":true}"#)
    });

    let client = RunnerClient::new(&server.base_url).unwrap();
    client.interrupt("user interrupt").await.unwrap();
    assert_eq!(server.recorded().len(), 1);
}

#[tokio::test]
async fn interrupt_surfaces_server_errors() {
    let server = serve(|_req| Response::json(400, r#"{"error":"expected an interrupt frame"}"#));
    let client = RunnerClient::new(&server.base_url).unwrap();
    let err = client.interrupt("x").await.unwrap_err();
    assert!(err.to_string().contains("400"), "{err}");
}
