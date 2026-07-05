//! Integration: the runner client against a wire-faithful NDJSON stream.

mod support;

use agentos::runner::RunnerClient;
use agentos_aci_protocol::{EventType, OutboundEvent, SessionStatus, PROTOCOL_VERSION};
use support::{serve, Response};

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
