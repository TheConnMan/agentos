//! `agentos chat`: drive the whole system end to end with no Slack at all.
//!
//! The CLI *is* the Slack service. It stands up a minimal Slack Web API stub on
//! a local port, XADDs the exact `QueuedSlackEvent` the dispatcher would produce
//! onto the real Valkey stream (synthetic, internally-consistent ids since the
//! CLI itself is the endpoint that receives them back), then waits for the
//! worker to consume and finalize the turn. It prints the placeholder's final
//! text and exits 0, or on timeout prints stream diagnostics and exits nonzero.
//!
//! Completion is the worker's XACK of our stream entry, not a timing guess: the
//! worker acks an entry only after the turn finalizes (its last `chat.update`
//! edit lands before the ack), so an acked entry means the turn is done and the
//! latest captured edit is the final reply. This avoids reporting a throttled
//! interim edit as the answer.
//!
//! The worker must run with `SLACK_API_BASE_URL` pointing at this stub's `/api/`
//! base: the worker reads that env var (`agentos_worker.config`) and builds its
//! Slack sink's `AsyncWebClient(token=..., base_url=...)` against it, so its
//! `chat.update` edits land at this stub instead of real Slack. No Slack token,
//! channel, or real Slack HTTP on the CLI side.

use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use axum::extract::{Path, State};
use axum::http::header::CONTENT_TYPE;
use axum::http::HeaderMap;
use axum::routing::post;
use axum::{Json, Router};
use serde_json::json;
use tokio::net::TcpListener;
use tokio::sync::mpsc;

use crate::queue::{
    self, connect, diagnostics, entry_acked, synthetic_channel, synthetic_thread_and_placeholder,
    xadd, QueuedSlackEvent, WORKER_GROUP,
};

pub const DEFAULT_STREAM: &str = queue::DEFAULT_STREAM;
pub const DEFAULT_VALKEY_URL: &str = queue::DEFAULT_VALKEY_URL;
pub const DEFAULT_USER: &str = "U-agentos-chat";
pub const DEFAULT_TIMEOUT_SECS: u64 = 180;
pub const DEFAULT_LISTEN_HOST: &str = "localhost";
pub const DEFAULT_LISTEN_PORT: u16 = 0;

/// How often we check whether the worker has acked our entry.
const ACK_POLL_INTERVAL: Duration = Duration::from_millis(500);
/// Bounded drain after the ack to catch a final edit still in flight.
const FINAL_DRAIN: Duration = Duration::from_millis(100);

/// Options for `agentos chat`, mirroring its clap flags.
pub struct ChatOpts {
    pub text: String,
    pub valkey_url: String,
    pub stream: String,
    pub user: String,
    pub timeout_secs: u64,
    pub listen_host: String,
    pub listen_port: u16,
}

/// One captured Slack Web API call at the stub.
#[derive(Debug, Clone)]
pub struct SlackCall {
    pub method: String,
    pub channel: Option<String>,
    pub ts: Option<String>,
    pub text: Option<String>,
}

/// If this call is a `chat.update` editing `placeholder_ts`, its new text.
pub fn placeholder_update_text<'a>(call: &'a SlackCall, placeholder_ts: &str) -> Option<&'a str> {
    if call.method == "chat.update" && call.ts.as_deref() == Some(placeholder_ts) {
        call.text.as_deref()
    } else {
        None
    }
}

/// Extract `channel`, `ts`, `text` from a Slack Web API request body, which
/// slack_sdk sends form-urlencoded by default and JSON when a param is complex.
pub fn extract_fields(
    content_type: &str,
    body: &str,
) -> (Option<String>, Option<String>, Option<String>) {
    if content_type.contains("application/json") {
        if let Ok(value) = serde_json::from_str::<serde_json::Value>(body) {
            let get = |k: &str| {
                value
                    .get(k)
                    .and_then(serde_json::Value::as_str)
                    .map(str::to_string)
            };
            return (get("channel"), get("ts"), get("text"));
        }
    }
    let pairs: Vec<(String, String)> = serde_urlencoded::from_str(body).unwrap_or_default();
    let find = |k: &str| {
        pairs
            .iter()
            .find(|(key, _)| key == k)
            .map(|(_, v)| v.clone())
    };
    (find("channel"), find("ts"), find("text"))
}

#[derive(Clone)]
struct StubState {
    tx: mpsc::UnboundedSender<SlackCall>,
}

/// The embedded Slack Web API stub. Serves `POST /api/<method>` for any method,
/// captures the call, and answers `{"ok": true, ...}` so the worker's slack_sdk
/// never errors. A real Slack service does not 404 the worker, so the stub is
/// permissive by design.
pub struct SlackStub {
    base_api_url: String,
    calls: mpsc::UnboundedReceiver<SlackCall>,
    server: tokio::task::JoinHandle<()>,
}

impl Drop for SlackStub {
    fn drop(&mut self) {
        self.server.abort();
    }
}

impl SlackStub {
    pub async fn start(host: &str, port: u16) -> Result<Self> {
        let listener = TcpListener::bind(format!("{host}:{port}"))
            .await
            .with_context(|| format!("binding the Slack stub on {host}:{port}"))?;
        let addr = listener
            .local_addr()
            .context("reading the stub's local addr")?;
        let (tx, calls) = mpsc::unbounded_channel();
        let app = Router::new()
            .route("/api/{method}", post(handle_call))
            .with_state(StubState { tx });
        let server = tokio::spawn(async move {
            let _ = axum::serve(listener, app).await;
        });
        Ok(Self {
            base_api_url: format!("http://{addr}/api/"),
            calls,
            server,
        })
    }

    /// The `SLACK_API_BASE_URL` the worker must point at.
    pub fn base_api_url(&self) -> &str {
        &self.base_api_url
    }

    /// Await the next captured call, or `None` if the stub has shut down.
    pub async fn recv(&mut self) -> Option<SlackCall> {
        self.calls.recv().await
    }
}

async fn handle_call(
    State(state): State<StubState>,
    Path(method): Path<String>,
    headers: HeaderMap,
    body: String,
) -> Json<serde_json::Value> {
    let content_type = headers
        .get(CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("");
    let (channel, ts, text) = extract_fields(content_type, &body);
    // chat.update echoes the existing ts; a hypothetical new-message call has no
    // ts, so synthesize one so the response still looks like Slack.
    let ts_out = ts
        .clone()
        .unwrap_or_else(|| synthetic_thread_and_placeholder().0);
    let _ = state.tx.send(SlackCall {
        method,
        channel: channel.clone(),
        ts: ts.clone(),
        text: text.clone(),
    });
    Json(json!({ "ok": true, "ts": ts_out, "channel": channel, "text": text }))
}

enum Outcome {
    /// The worker finished the turn; the final placeholder text.
    Replied(String),
    /// The worker finished the turn but never edited the placeholder.
    CompletedNoEdit,
    /// The deadline passed with no completion.
    TimedOut,
}

/// Wait for the worker to consume and finalize the turn. Terminal signal is the
/// XACK of our entry; the reply is the latest `chat.update` we captured.
async fn await_reply(
    stub: &mut SlackStub,
    conn: &mut redis::aio::MultiplexedConnection,
    stream: &str,
    entry_id: &str,
    placeholder_ts: &str,
    timeout: Duration,
) -> Outcome {
    let deadline = Instant::now() + timeout;
    let mut latest: Option<String> = None;
    let mut poll = tokio::time::interval(ACK_POLL_INTERVAL);
    loop {
        tokio::select! {
            call = stub.recv() => {
                if let Some(call) = call {
                    if let Some(text) = placeholder_update_text(&call, placeholder_ts) {
                        latest = Some(text.to_string());
                    }
                }
            }
            _ = poll.tick() => {
                if entry_acked(conn, stream, WORKER_GROUP, entry_id).await {
                    // Drain any final edit still in flight before deciding.
                    while let Ok(Some(call)) = tokio::time::timeout(FINAL_DRAIN, stub.recv()).await {
                        if let Some(text) = placeholder_update_text(&call, placeholder_ts) {
                            latest = Some(text.to_string());
                        }
                    }
                    return latest.map_or(Outcome::CompletedNoEdit, Outcome::Replied);
                }
            }
            _ = tokio::time::sleep_until(tokio::time::Instant::from_std(deadline)) => {
                return Outcome::TimedOut;
            }
        }
    }
}

/// The `agentos chat` handler.
pub async fn chat(opts: ChatOpts) -> Result<()> {
    // Connect Valkey up front so a misconfigured stream or down stack fails fast,
    // before the stub binds or any id is minted.
    let mut conn = connect(&opts.valkey_url).await?;

    let mut stub = SlackStub::start(&opts.listen_host, opts.listen_port).await?;
    println!(
        "slack stub listening; run the worker with SLACK_API_BASE_URL={}",
        stub.base_api_url()
    );

    // Invent internally-consistent synthetic ids; the CLI is both the producer
    // and the Slack endpoint that receives them back.
    let channel = synthetic_channel();
    let (thread_ts, placeholder_ts) = synthetic_thread_and_placeholder();
    let event = QueuedSlackEvent::synthetic(
        &channel,
        &opts.user,
        &opts.text,
        &thread_ts,
        &placeholder_ts,
    );
    let stream_id = xadd(&mut conn, &opts.stream, &event).await?;
    println!(
        "enqueued {} on {} as {stream_id}",
        event.slack_event_id, opts.stream
    );
    println!(
        "waiting up to {}s for the worker to finalize the turn...",
        opts.timeout_secs
    );

    let outcome = await_reply(
        &mut stub,
        &mut conn,
        &opts.stream,
        &stream_id,
        &placeholder_ts,
        Duration::from_secs(opts.timeout_secs),
    )
    .await;

    match outcome {
        Outcome::Replied(reply) => {
            println!("reply    {reply}");
            Ok(())
        }
        Outcome::CompletedNoEdit => {
            println!("the worker finished the turn but never edited the placeholder");
            Ok(())
        }
        Outcome::TimedOut => {
            println!(
                "TIMEOUT: the worker did not finalize within {}s. Stream diagnostics:",
                opts.timeout_secs
            );
            let diag = diagnostics(&mut conn, &opts.stream, &stream_id).await;
            println!("{diag}");
            std::process::exit(1);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extract_fields_reads_form_and_json_bodies() {
        let (channel, ts, text) = extract_fields(
            "application/x-www-form-urlencoded",
            "token=xoxb-x&channel=C1&ts=1.2&text=hi+there",
        );
        assert_eq!(channel.as_deref(), Some("C1"));
        assert_eq!(ts.as_deref(), Some("1.2"));
        assert_eq!(text.as_deref(), Some("hi there"));

        let (channel, ts, text) = extract_fields(
            "application/json; charset=utf-8",
            r#"{"channel":"C2","ts":"3.4","text":"done"}"#,
        );
        assert_eq!(channel.as_deref(), Some("C2"));
        assert_eq!(ts.as_deref(), Some("3.4"));
        assert_eq!(text.as_deref(), Some("done"));
    }

    #[test]
    fn placeholder_update_text_matches_only_the_right_call() {
        let update = SlackCall {
            method: "chat.update".into(),
            channel: Some("C1".into()),
            ts: Some("1.2".into()),
            text: Some("the answer".into()),
        };
        assert_eq!(placeholder_update_text(&update, "1.2"), Some("the answer"));
        // Wrong ts (a different message).
        assert_eq!(placeholder_update_text(&update, "9.9"), None);
        // Wrong method (a new-message post, not an edit).
        let post = SlackCall {
            method: "chat.postMessage".into(),
            ..update.clone()
        };
        assert_eq!(placeholder_update_text(&post, "1.2"), None);
    }
}
