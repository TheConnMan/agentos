//! Integration: the chat enqueue seam against real Valkey, and the embedded
//! Slack stub exercised over real HTTP.
//!
//! The redis client is NOT mocked: the XADD runs against the compose dev Valkey
//! (host port 56379, password `valkeypass`) on a unique test-scoped stream that
//! the test deletes afterward. It never touches the real `agentos:runs` stream.
//! The Slack stub is the real one; only its client (reqwest) stands in for the
//! worker.

use std::time::Duration;

use agentos::chat::SlackStub;
use agentos::queue::{diagnostics, entry_acked, xadd, QueuedSlackEvent, WORKER_GROUP};

const DEFAULT_VALKEY_URL: &str = "redis://:valkeypass@localhost:56379";

fn valkey_url() -> String {
    std::env::var("TEST_VALKEY_URL").unwrap_or_else(|_| DEFAULT_VALKEY_URL.to_string())
}

/// Connect and PING; return `None` (with a skip note) when Valkey is not
/// reachable, mirroring the dispatcher's `pytest.skip`. CI does not start the
/// compose stack, so the Valkey-backed tests skip there rather than failing.
async fn valkey_or_skip(test: &str) -> Option<redis::aio::MultiplexedConnection> {
    let url = valkey_url();
    let connect = async {
        let client = redis::Client::open(url.clone())?;
        let mut conn = client.get_multiplexed_async_connection().await?;
        let _: String = redis::cmd("PING").query_async(&mut conn).await?;
        redis::RedisResult::Ok(conn)
    };
    match connect.await {
        Ok(conn) => Some(conn),
        Err(err) => {
            eprintln!("skipping {test}: Valkey not reachable at {url}: {err}");
            None
        }
    }
}

fn unique_stream() -> String {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    format!("agentos:test:chat:{nanos}")
}

#[tokio::test]
async fn xadd_lands_the_exact_seam_shape_on_real_valkey() {
    let Some(mut conn) = valkey_or_skip("xadd_lands_the_exact_seam_shape_on_real_valkey").await
    else {
        return;
    };
    let stream = unique_stream();

    let event = QueuedSlackEvent::synthetic(
        "C-SIM-x",
        "U-agentos-chat",
        "hello world",
        "111.100",
        "111.200",
    );
    let stream_id = xadd(&mut conn, &stream, &event).await.unwrap();
    assert!(!stream_id.is_empty(), "XADD returned an id");

    // Read the entry back with XRANGE: [ (id, [field, value, ...]) ].
    let entries: Vec<(String, Vec<(String, String)>)> = redis::cmd("XRANGE")
        .arg(&stream)
        .arg("-")
        .arg("+")
        .query_async(&mut conn)
        .await
        .unwrap();

    // Clean up before asserting so a failure never leaks a test stream.
    let _: i64 = redis::cmd("DEL")
        .arg(&stream)
        .query_async(&mut conn)
        .await
        .unwrap();

    assert_eq!(entries.len(), 1, "exactly one entry enqueued");
    let (entry_id, fields) = &entries[0];
    assert_eq!(entry_id, &stream_id, "read-back id matches the XADD id");

    // The single-`payload`-field encoding is the frozen seam.
    assert_eq!(fields.len(), 1, "exactly one field on the entry");
    assert_eq!(fields[0].0, "payload", "the field is named payload");

    // The payload JSON round-trips into the same event with the exact
    // dispatcher-side field names.
    let decoded: QueuedSlackEvent = serde_json::from_str(&fields[0].1).unwrap();
    assert_eq!(decoded, event);

    let value: serde_json::Value = serde_json::from_str(&fields[0].1).unwrap();
    let mut keys: Vec<&str> = value
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect();
    keys.sort_unstable();
    assert_eq!(
        keys,
        vec![
            "channel",
            "placeholder_ts",
            "received_at",
            "slack_event_id",
            "text",
            "thread_ts",
            "user",
        ]
    );
    assert!(
        decoded.slack_event_id.starts_with("EvSIM-"),
        "synthetic id keeps its collision-proof prefix on the wire"
    );
}

#[tokio::test]
async fn diagnostics_reports_stream_length_and_consumer_group_state() {
    // The timeout path's value is showing whether a consumer group consumed the
    // entry. Prove it against real Valkey with a real group over a test stream.
    let Some(mut conn) =
        valkey_or_skip("diagnostics_reports_stream_length_and_consumer_group_state").await
    else {
        return;
    };
    let stream = unique_stream();

    let event = QueuedSlackEvent::synthetic("C-SIM-x", "U-agentos-chat", "hi", "1.1", "1.2");
    let stream_id = xadd(&mut conn, &stream, &event).await.unwrap();

    // The worker's group name; create it at 0 so it sees the existing entry.
    let _: () = redis::cmd("XGROUP")
        .arg("CREATE")
        .arg(&stream)
        .arg("agentos-workers")
        .arg("0")
        .query_async(&mut conn)
        .await
        .unwrap();

    let report = diagnostics(&mut conn, &stream, &stream_id).await;

    let _: i64 = redis::cmd("DEL")
        .arg(&stream)
        .query_async(&mut conn)
        .await
        .unwrap();

    assert!(report.contains(&stream), "names the stream:\n{report}");
    assert!(report.contains(&stream_id), "names our entry:\n{report}");
    assert!(report.contains("XLEN 1"), "reports length:\n{report}");
    assert!(
        report.contains("agentos-workers"),
        "surfaces the consumer group:\n{report}"
    );
    assert!(
        report.contains("XPENDING"),
        "includes pending state:\n{report}"
    );
}

#[tokio::test]
async fn entry_acked_tracks_the_worker_consuming_and_acking() {
    // chat's completion signal: our entry is "done" only once the worker group
    // has delivered AND acked it. Drive the group lifecycle by hand and assert
    // entry_acked flips exactly at the ack.
    let Some(mut conn) = valkey_or_skip("entry_acked_tracks_the_worker_consuming_and_acking").await
    else {
        return;
    };
    let stream = unique_stream();
    let event = QueuedSlackEvent::synthetic("C-SIM-x", "U-agentos-chat", "hi", "1.1", "1.2");
    let stream_id = xadd(&mut conn, &stream, &event).await.unwrap();

    // No group yet: not acked.
    assert!(!entry_acked(&mut conn, &stream, WORKER_GROUP, &stream_id).await);

    let _: () = redis::cmd("XGROUP")
        .arg("CREATE")
        .arg(&stream)
        .arg(WORKER_GROUP)
        .arg("0")
        .query_async(&mut conn)
        .await
        .unwrap();
    // Group exists but has not delivered the entry: not acked.
    assert!(!entry_acked(&mut conn, &stream, WORKER_GROUP, &stream_id).await);

    // The worker reads the entry (delivered, now pending-unacked): still not done.
    let _: redis::Value = redis::cmd("XREADGROUP")
        .arg("GROUP")
        .arg(WORKER_GROUP)
        .arg("worker-1")
        .arg("COUNT")
        .arg(10)
        .arg("STREAMS")
        .arg(&stream)
        .arg(">")
        .query_async(&mut conn)
        .await
        .unwrap();
    assert!(!entry_acked(&mut conn, &stream, WORKER_GROUP, &stream_id).await);

    // The worker acks after finalizing the turn: now done.
    let _: i64 = redis::cmd("XACK")
        .arg(&stream)
        .arg(WORKER_GROUP)
        .arg(&stream_id)
        .query_async(&mut conn)
        .await
        .unwrap();
    let acked = entry_acked(&mut conn, &stream, WORKER_GROUP, &stream_id).await;

    let _: i64 = redis::cmd("DEL")
        .arg(&stream)
        .query_async(&mut conn)
        .await
        .unwrap();

    assert!(
        acked,
        "entry_acked must be true once the group has acked the entry"
    );
}

#[tokio::test]
async fn stub_captures_form_encoded_chat_update_over_real_http() {
    // slack_sdk posts chat.update form-encoded by default; the stub must parse
    // it, capture it, and answer ok:true so the worker's SDK never errors.
    let mut stub = SlackStub::start("localhost", 0).await.unwrap();
    let base = stub.base_api_url().to_string();
    assert!(base.ends_with("/api/"), "base url: {base}");

    let http = reqwest::Client::new();
    let resp: serde_json::Value = http
        .post(format!("{base}chat.update"))
        .form(&[
            ("channel", "C1"),
            ("ts", "1720000000.000200"),
            ("text", "the answer"),
        ])
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(resp["ok"], true);
    assert_eq!(resp["ts"], "1720000000.000200");

    let call = tokio::time::timeout(Duration::from_secs(2), stub.recv())
        .await
        .expect("stub captured a call in time")
        .expect("stub is still up");
    assert_eq!(call.method, "chat.update");
    assert_eq!(call.channel.as_deref(), Some("C1"));
    assert_eq!(call.ts.as_deref(), Some("1720000000.000200"));
    assert_eq!(call.text.as_deref(), Some("the answer"));
}

#[tokio::test]
async fn stub_captures_json_body_and_never_404s_other_methods() {
    let mut stub = SlackStub::start("localhost", 0).await.unwrap();
    let base = stub.base_api_url().to_string();
    let http = reqwest::Client::new();

    // A JSON-bodied call to an unexpected method (e.g. a future postMessage)
    // must still be captured and answered ok, not 404'd.
    let resp = http
        .post(format!("{base}chat.postMessage"))
        .json(&serde_json::json!({"channel": "C2", "text": "escalation"}))
        .send()
        .await
        .unwrap();
    assert!(resp.status().is_success());
    let body: serde_json::Value = resp.json().await.unwrap();
    assert_eq!(body["ok"], true);

    let call = tokio::time::timeout(Duration::from_secs(2), stub.recv())
        .await
        .expect("stub captured a call in time")
        .expect("stub is still up");
    assert_eq!(call.method, "chat.postMessage");
    assert_eq!(call.channel.as_deref(), Some("C2"));
    assert_eq!(call.text.as_deref(), Some("escalation"));
}
