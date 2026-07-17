//! Shared Slack-stub machinery for driving the whole system end to end with no
//! Slack at all. Used by the `local message` and `cluster message` verbs.
//!
//! The CLI *is* the Slack service. It stands up a minimal Slack Web API stub on
//! a local port, XADDs the exact `QueuedTurn` the dispatcher would produce
//! onto the real Valkey stream (synthetic, internally-consistent ids since the
//! CLI itself is the endpoint that receives them back), then waits for the
//! worker to consume and finalize the turn. The caller prints the placeholder's
//! final text and exits 0, or on timeout prints stream diagnostics and exits
//! nonzero.
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
    self, entry_acked, synthetic_channel, synthetic_thread_and_placeholder, WORKER_GROUP,
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

/// The Block Kit action id on the approval card's Approve button
/// (`agentos_dispatcher.approval_actions.APPROVE_ACTION_ID`). Its presence in a
/// captured Slack call body is the unambiguous signal that a turn parked
/// awaiting approval and posted a card (#529).
pub const APPROVE_ACTION_ID: &str = "agentos-approval-approve";

/// One captured Slack Web API call at the stub.
#[derive(Debug, Clone)]
pub struct SlackCall {
    pub method: String,
    pub channel: Option<String>,
    pub ts: Option<String>,
    pub text: Option<String>,
    /// True when the raw body carried the approval card's Approve action id, i.e.
    /// the worker parked this turn awaiting approval and posted a card (#529).
    pub approval_card: bool,
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
    /// Bind the stub on `bind_host:port` but advertise its base URL under
    /// `advertise_host`. `chat` passes the same value for both (it binds and is
    /// reached on the same local host); `message` binds `0.0.0.0` so an
    /// in-cluster worker can reach it, and advertises the routable host it
    /// detected. The advertised port is the actually-bound port, so an ephemeral
    /// `port: 0` still produces a correct URL.
    pub async fn start(bind_host: &str, port: u16, advertise_host: &str) -> Result<Self> {
        let listener = TcpListener::bind(format!("{bind_host}:{port}"))
            .await
            .with_context(|| format!("binding the Slack stub on {bind_host}:{port}"))?;
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
            base_api_url: format!("http://{advertise_host}:{}/api/", addr.port()),
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
    // The approval card's Approve button carries this action id in the blocks,
    // which `extract_fields` does not parse -- match it on the raw body so an
    // awaiting-approval turn is detectable regardless of encoding (#529).
    let approval_card = body.contains(APPROVE_ACTION_ID);
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
        approval_card,
    });
    Json(json!({ "ok": true, "ts": ts_out, "channel": channel, "text": text }))
}

pub enum Outcome {
    /// The worker finished the turn; the final placeholder text.
    Replied(String),
    /// The worker finished the turn but never edited the placeholder.
    CompletedNoEdit,
    /// The turn parked awaiting human approval: the worker posted an approval
    /// card (rather than finalizing) and persisted a durable `Approval` carrying
    /// THIS run's reply endpoint. For `local`/`cluster message` that endpoint is
    /// the CLI's throwaway stub, which dies when the command exits, so the resumed
    /// reply has nowhere to land (#529). Carries the latest placeholder text seen.
    AwaitingApproval(Option<String>),
    /// The deadline passed with no completion.
    TimedOut,
}

/// Wait for the worker to consume and finalize the turn. Terminal signal is the
/// XACK of our entry; the reply is the latest `chat.update` we captured. Shared
/// by `chat` and `message` (both stand up the same stub + enqueue + ack seam).
pub async fn await_reply(
    stub: &mut SlackStub,
    conn: &mut redis::aio::MultiplexedConnection,
    stream: &str,
    entry_id: &str,
    placeholder_ts: &str,
    timeout: Duration,
) -> Outcome {
    let deadline = Instant::now() + timeout;
    let mut latest: Option<String> = None;
    // Whether the worker posted an approval card during this turn: the turn parked
    // awaiting approval rather than finalizing normally (#529).
    let mut awaiting_approval = false;
    let mut poll = tokio::time::interval(ACK_POLL_INTERVAL);
    loop {
        tokio::select! {
            call = stub.recv() => {
                if let Some(call) = call {
                    awaiting_approval |= call.approval_card;
                    if let Some(text) = placeholder_update_text(&call, placeholder_ts) {
                        latest = Some(text.to_string());
                    }
                }
            }
            _ = poll.tick() => {
                if entry_acked(conn, stream, WORKER_GROUP, entry_id).await {
                    // Drain any final edit still in flight before deciding.
                    while let Ok(Some(call)) = tokio::time::timeout(FINAL_DRAIN, stub.recv()).await {
                        awaiting_approval |= call.approval_card;
                        if let Some(text) = placeholder_update_text(&call, placeholder_ts) {
                            latest = Some(text.to_string());
                        }
                    }
                    if awaiting_approval {
                        return Outcome::AwaitingApproval(latest);
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

pub fn continue_hint_line(verb: &str) -> String {
    format!("continue this conversation: agentos {verb} --continue \"...\"")
}

pub fn continue_hint_long_line(verb: &str, channel: &str, thread_ts: &str) -> String {
    format!(
        "continue this conversation: agentos {verb} --channel '{channel}' --thread {thread_ts} \"...\""
    )
}

/// Resolve the channel and timestamps for a turn: an explicit `--channel` is
/// carried verbatim (so the worker's exact-equality binding can route to a
/// deployed agent) and an explicit `--thread` continues that thread; each falls
/// back to a fresh synthetic value when absent. The placeholder ts is always
/// synthetic (the CLI owns the placeholder message). Returns
/// `(channel, thread_ts, placeholder_ts)`.
pub fn resolve_targets(channel: Option<&str>, thread: Option<&str>) -> (String, String, String) {
    let channel = channel.map_or_else(synthetic_channel, str::to_string);
    let (synthetic_thread_ts, placeholder_ts) = synthetic_thread_and_placeholder();
    let thread_ts = thread.map_or(synthetic_thread_ts, str::to_string);
    (channel, thread_ts, placeholder_ts)
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
            approval_card: false,
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

    #[test]
    fn continue_hint_is_the_short_form() {
        let line = continue_hint_line("cluster message");

        assert!(line.contains("--continue"));
        assert!(line.contains("agentos cluster message"));
        assert!(!line.contains("--channel"));
        assert!(!line.contains("--thread"));
    }

    #[test]
    fn long_fallback_hint_quotes_the_channel() {
        let line = continue_hint_long_line("local message", "#local-dev", "123.45");

        assert!(line.contains("'#local-dev'"));
        assert!(line.contains("--thread 123.45"));
    }
}
