//! Integration: the slack-sim enqueue seam against real Valkey, and the Slack
//! client leg against a wire-level mock.
//!
//! The redis client is NOT mocked: the XADD runs against the compose dev Valkey
//! (host port 56379, password `valkeypass`) on a unique test-scoped stream that
//! the test deletes afterward. It never touches the real `agentos:runs` stream.
//! Slack HTTP is the only mocked seam.

mod support;

use std::time::{Duration, Instant};

use agentos::queue::{xadd, QueuedSlackEvent};
use agentos::slack_sim::{
    reply_text_if_changed, wait_for_completion, SimOutcome, SlackClient, PLACEHOLDER_TEXT,
};
use support::{serve, Response};

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
    format!("agentos:test:slack-sim:{nanos}")
}

#[tokio::test]
async fn xadd_lands_the_exact_seam_shape_on_real_valkey() {
    let Some(mut conn) = valkey_or_skip("xadd_lands_the_exact_seam_shape_on_real_valkey").await
    else {
        return;
    };
    let stream = unique_stream();

    let event =
        QueuedSlackEvent::synthetic("C-test", "U-slack-sim", "hello world", "111.100", "111.200");
    let stream_id = xadd(&mut conn, &stream, &event).await.unwrap();
    assert!(!stream_id.is_empty(), "XADD returned an id");

    let entries: Vec<(String, Vec<(String, String)>)> = redis::cmd("XRANGE")
        .arg(&stream)
        .arg("-")
        .arg("+")
        .query_async(&mut conn)
        .await
        .unwrap();

    let _: i64 = redis::cmd("DEL")
        .arg(&stream)
        .query_async(&mut conn)
        .await
        .unwrap();

    assert_eq!(entries.len(), 1, "exactly one entry enqueued");
    let (entry_id, fields) = &entries[0];
    assert_eq!(entry_id, &stream_id, "read-back id matches the XADD id");
    assert_eq!(fields.len(), 1, "exactly one field on the entry");
    assert_eq!(fields[0].0, "payload", "the field is named payload");

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
    assert!(decoded.slack_event_id.starts_with("EvSIM-"));
}

#[tokio::test]
async fn slack_client_posts_root_and_threaded_placeholder_with_auth() {
    // Mock ONLY the Slack HTTP seam. The client under test is the real one.
    let server = serve(|req| {
        assert_eq!(req.header("authorization"), Some("Bearer xoxb-test"));
        assert!(
            req.path.starts_with("/chat.postMessage"),
            "path: {}",
            req.path
        );
        let body: serde_json::Value = serde_json::from_slice(&req.body).unwrap();
        if body.get("thread_ts").is_some() {
            assert_eq!(body["thread_ts"], "1720000000.000100");
            assert_eq!(body["text"], PLACEHOLDER_TEXT);
            Response::json(200, r#"{"ok":true,"ts":"1720000000.000200"}"#)
        } else {
            assert_eq!(body["channel"], "C-test");
            assert_eq!(body["text"], "the user text");
            Response::json(200, r#"{"ok":true,"ts":"1720000000.000100"}"#)
        }
    });

    let slack = SlackClient::new(&server.base_url, "xoxb-test").unwrap();
    let root_ts = slack
        .post_message("C-test", "the user text", None)
        .await
        .unwrap();
    assert_eq!(root_ts, "1720000000.000100");
    let placeholder_ts = slack
        .post_message("C-test", PLACEHOLDER_TEXT, Some(&root_ts))
        .await
        .unwrap();
    assert_eq!(placeholder_ts, "1720000000.000200");
    assert_eq!(server.recorded().len(), 2);
}

#[tokio::test]
async fn slack_client_reads_replies_and_detects_the_edit() {
    let server = serve(|req| {
        assert_eq!(req.method, "GET");
        assert!(
            req.path.starts_with("/conversations.replies"),
            "path: {}",
            req.path
        );
        assert!(req.path.contains("channel=C-test"));
        assert!(req.path.contains("ts=1720000000.000100"));
        Response::json(
            200,
            r#"{"ok":true,"messages":[
                {"ts":"1720000000.000100","text":"the user text"},
                {"ts":"1720000000.000200","text":"Here is the worker answer."}
            ]}"#,
        )
    });

    let slack = SlackClient::new(&server.base_url, "xoxb-test").unwrap();
    let messages = slack
        .conversations_replies("C-test", "1720000000.000100")
        .await
        .unwrap();
    let reply = reply_text_if_changed(&messages, "1720000000.000200", PLACEHOLDER_TEXT);
    assert_eq!(reply.as_deref(), Some("Here is the worker answer."));
}

#[tokio::test]
async fn slack_client_surfaces_api_errors() {
    let server = serve(|_req| Response::json(200, r#"{"ok":false,"error":"channel_not_found"}"#));
    let slack = SlackClient::new(&server.base_url, "xoxb-test").unwrap();
    let err = slack.post_message("C-nope", "hi", None).await.unwrap_err();
    assert!(err.to_string().contains("channel_not_found"), "{err}");
}

#[tokio::test]
async fn wait_for_completion_holds_until_the_worker_acks() {
    // Even though conversations.replies already shows the placeholder edited, the
    // wait must not return until the worker acks the entry -- a throttled interim
    // edit is not the final answer. Drive a real ack ~300ms in and assert the
    // wait returns the edited text only after it.
    let Some(mut conn) = valkey_or_skip("wait_for_completion_holds_until_the_worker_acks").await
    else {
        return;
    };
    let stream = unique_stream();
    let event = QueuedSlackEvent::synthetic("C-test", "U-slack-sim", "hi", "111.100", "111.200");
    let stream_id = xadd(&mut conn, &stream, &event).await.unwrap();

    // Deliver the entry to the worker group so it becomes acknowledgeable.
    let _: () = redis::cmd("XGROUP")
        .arg("CREATE")
        .arg(&stream)
        .arg("agentos-workers")
        .arg("0")
        .query_async(&mut conn)
        .await
        .unwrap();
    let _: redis::Value = redis::cmd("XREADGROUP")
        .arg("GROUP")
        .arg("agentos-workers")
        .arg("worker-1")
        .arg("COUNT")
        .arg(10)
        .arg("STREAMS")
        .arg(&stream)
        .arg(">")
        .query_async(&mut conn)
        .await
        .unwrap();

    // The mock always reports the placeholder already edited to the final text.
    let server = serve(|_req| {
        Response::json(
            200,
            r#"{"ok":true,"messages":[
                {"ts":"111.100","text":"the user text"},
                {"ts":"111.200","text":"the final answer"}
            ]}"#,
        )
    });
    let slack = SlackClient::new(&server.base_url, "xoxb-test").unwrap();

    // A separate connection acks the entry after a delay (the "worker finalizing").
    let ack_stream = stream.clone();
    let ack_id = stream_id.clone();
    let acker = tokio::spawn(async move {
        let client = redis::Client::open(valkey_url()).unwrap();
        let mut c = client.get_multiplexed_async_connection().await.unwrap();
        tokio::time::sleep(Duration::from_millis(300)).await;
        let _: i64 = redis::cmd("XACK")
            .arg(&ack_stream)
            .arg("agentos-workers")
            .arg(&ack_id)
            .query_async(&mut c)
            .await
            .unwrap();
    });

    let started = Instant::now();
    let outcome = wait_for_completion(
        &slack,
        &mut conn,
        &stream,
        &stream_id,
        "C-test",
        "111.100",
        "111.200",
        PLACEHOLDER_TEXT,
        Duration::from_secs(10),
    )
    .await
    .unwrap();
    let elapsed = started.elapsed();
    acker.await.unwrap();

    let _: i64 = redis::cmd("DEL")
        .arg(&stream)
        .query_async(&mut conn)
        .await
        .unwrap();

    assert_eq!(outcome, SimOutcome::Replied("the final answer".to_string()));
    assert!(
        elapsed >= Duration::from_millis(250),
        "must wait for the ack, not return on the first text change (elapsed {elapsed:?})"
    );
}
