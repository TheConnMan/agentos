//! `agentos slack-sim`: the real-Slack egress leg without Socket Mode.
//!
//! It posts a synthetic conversation in a real Slack channel as the bot (a root
//! message rendering the simulated user text, then a threaded placeholder),
//! enqueues the exact `QueuedSlackEvent` the dispatcher would produce onto the
//! real Valkey stream, and waits for the worker to finalize the turn. On success
//! it prints the reply and exits 0; on timeout it prints stream diagnostics and
//! exits nonzero.
//!
//! Completion is the worker's XACK of our stream entry, not the first placeholder
//! edit: the worker throttles live `chat.update` edits during a turn and acks
//! only after finalizing, so waiting for the ack (then reading the latest
//! placeholder text) avoids reporting a throttled interim edit as the answer.
//! This is the same completion signal `chat` uses; both share it via `queue`.
//!
//! This is the real-Slack rung of the ladder; the no-Slack rung is `chat`, which
//! makes the CLI itself the Slack service.

use std::time::{Duration, Instant};

use anyhow::{bail, Context, Result};
use redis::aio::MultiplexedConnection;
use serde::Deserialize;

use crate::queue::{self, connect, diagnostics, entry_acked, xadd, QueuedSlackEvent, WORKER_GROUP};

pub const DEFAULT_STREAM: &str = queue::DEFAULT_STREAM;
pub const DEFAULT_VALKEY_URL: &str = queue::DEFAULT_VALKEY_URL;
pub const DEFAULT_USER: &str = "U-slack-sim";
pub const DEFAULT_TIMEOUT_SECS: u64 = 180;

/// The threaded placeholder text the sim posts and the worker later edits away.
pub const PLACEHOLDER_TEXT: &str = "On it.";

const SLACK_API_BASE: &str = "https://slack.com/api";
const POLL_INTERVAL: Duration = Duration::from_secs(2);

/// Options for `agentos slack-sim`, mirroring its clap flags.
pub struct SlackSimOpts {
    pub text: String,
    pub channel: String,
    pub bot_token: String,
    pub valkey_url: String,
    pub stream: String,
    pub user: String,
    pub timeout_secs: u64,
}

/// A Slack message as returned by `conversations.replies` (only the fields the
/// sim needs).
#[derive(Debug, Clone, Deserialize)]
pub struct SlackMessage {
    pub ts: String,
    #[serde(default)]
    pub text: String,
}

/// If the placeholder message (matched by `ts`) now carries text other than the
/// original placeholder, return that text; otherwise `None` (still pending).
pub fn reply_text_if_changed(
    messages: &[SlackMessage],
    placeholder_ts: &str,
    original: &str,
) -> Option<String> {
    messages
        .iter()
        .find(|m| m.ts == placeholder_ts)
        .filter(|m| !m.text.is_empty() && m.text != original)
        .map(|m| m.text.clone())
}

/// A Slack Web API call returned `ok: false`. `code` is the raw Slack error
/// string (e.g. `missing_scope`), preserved so callers can classify recoverable
/// failures without string-matching a formatted message.
#[derive(Debug, Clone)]
pub struct SlackApiError {
    pub method: &'static str,
    pub code: String,
}

impl std::fmt::Display for SlackApiError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{} failed: {}", self.method, self.code)
    }
}

impl std::error::Error for SlackApiError {}

/// Whether an error is the Slack `missing_scope` failure the reply-poll hits
/// when the bot token lacks `channels:history`/`groups:history`. The sim treats
/// this as a graceful degrade (post succeeded, only live polling is unavailable)
/// rather than a pipeline failure.
pub fn is_missing_scope(err: &anyhow::Error) -> bool {
    err.downcast_ref::<SlackApiError>()
        .is_some_and(|e| e.code == "missing_scope")
}

/// Minimal Slack Web API client over the shared reqwest dependency.
///
/// The base URL is injectable so integration tests can point it at a wire-level
/// mock; production always uses the real Slack API.
pub struct SlackClient {
    base: String,
    token: String,
    http: reqwest::Client,
}

#[derive(Deserialize)]
struct PostMessageResponse {
    ok: bool,
    error: Option<String>,
    ts: Option<String>,
}

#[derive(Deserialize)]
struct RepliesResponse {
    ok: bool,
    error: Option<String>,
    messages: Option<Vec<SlackMessage>>,
}

#[derive(Deserialize)]
struct PermalinkResponse {
    ok: bool,
    error: Option<String>,
    permalink: Option<String>,
}

impl SlackClient {
    pub fn new(base: &str, token: &str) -> Result<Self> {
        let http = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(10))
            .build()
            .context("building Slack HTTP client")?;
        Ok(Self {
            base: base.trim_end_matches('/').to_string(),
            token: token.to_string(),
            http,
        })
    }

    /// `chat.postMessage`; returns the new message ts. When `thread_ts` is set the
    /// message is a threaded reply.
    pub async fn post_message(
        &self,
        channel: &str,
        text: &str,
        thread_ts: Option<&str>,
    ) -> Result<String> {
        let mut body = serde_json::json!({"channel": channel, "text": text});
        if let Some(ts) = thread_ts {
            body["thread_ts"] = serde_json::Value::String(ts.to_string());
        }
        let resp: PostMessageResponse = self
            .http
            .post(format!("{}/chat.postMessage", self.base))
            .bearer_auth(&self.token)
            .json(&body)
            .send()
            .await
            .context("POST chat.postMessage")?
            .json()
            .await
            .context("decoding chat.postMessage response")?;
        if !resp.ok {
            bail!(
                "chat.postMessage failed: {}",
                resp.error.unwrap_or_else(|| "unknown error".into())
            );
        }
        resp.ts
            .context("chat.postMessage returned ok but no message ts")
    }

    /// `conversations.replies` for a thread; returns the thread's messages.
    pub async fn conversations_replies(
        &self,
        channel: &str,
        thread_ts: &str,
    ) -> Result<Vec<SlackMessage>> {
        let resp: RepliesResponse = self
            .http
            .get(format!("{}/conversations.replies", self.base))
            .bearer_auth(&self.token)
            .query(&[("channel", channel), ("ts", thread_ts)])
            .send()
            .await
            .context("GET conversations.replies")?
            .json()
            .await
            .context("decoding conversations.replies response")?;
        if !resp.ok {
            return Err(SlackApiError {
                method: "conversations.replies",
                code: resp.error.unwrap_or_else(|| "unknown error".into()),
            }
            .into());
        }
        Ok(resp.messages.unwrap_or_default())
    }

    /// `chat.getPermalink` for a message; best-effort (callers fall back to
    /// channel/ts on error).
    pub async fn permalink(&self, channel: &str, message_ts: &str) -> Result<String> {
        let resp: PermalinkResponse = self
            .http
            .get(format!("{}/chat.getPermalink", self.base))
            .bearer_auth(&self.token)
            .query(&[("channel", channel), ("message_ts", message_ts)])
            .send()
            .await
            .context("GET chat.getPermalink")?
            .json()
            .await
            .context("decoding chat.getPermalink response")?;
        if !resp.ok {
            bail!(
                "chat.getPermalink failed: {}",
                resp.error.unwrap_or_else(|| "unknown error".into())
            );
        }
        resp.permalink
            .context("chat.getPermalink returned ok but no permalink")
    }
}

/// The result of waiting for the worker to finalize the turn.
#[derive(Debug, PartialEq, Eq)]
pub enum SimOutcome {
    /// The worker acked the entry; the final placeholder text.
    Replied(String),
    /// The worker acked but the placeholder never changed from the placeholder.
    CompletedNoEdit,
    /// The reply-poll could not read the thread because the bot token lacks
    /// `channels:history`/`groups:history`. The turn still runs in Slack; only
    /// live polling here is unavailable.
    PollScopeMissing,
    /// The deadline passed with no ack.
    TimedOut,
}

/// Wait for the worker to consume and finalize the turn. Terminal signal is the
/// XACK of our entry; the reply is the latest edited placeholder text read from
/// `conversations.replies`. Polling keeps capturing the latest edit so the text
/// is current when the ack lands.
#[allow(clippy::too_many_arguments)]
pub async fn wait_for_completion(
    slack: &SlackClient,
    conn: &mut MultiplexedConnection,
    stream: &str,
    entry_id: &str,
    channel: &str,
    thread_ts: &str,
    placeholder_ts: &str,
    original: &str,
    timeout: Duration,
) -> Result<SimOutcome> {
    let deadline = Instant::now() + timeout;
    let mut latest: Option<String> = None;
    loop {
        let messages = match slack.conversations_replies(channel, thread_ts).await {
            Ok(messages) => messages,
            // The scope is fixed for the run, so a missing_scope failure will
            // recur every poll; surface it once as a graceful degrade.
            Err(e) if is_missing_scope(&e) => return Ok(SimOutcome::PollScopeMissing),
            Err(e) => return Err(e),
        };
        if let Some(text) = reply_text_if_changed(&messages, placeholder_ts, original) {
            latest = Some(text);
        }
        if entry_acked(conn, stream, WORKER_GROUP, entry_id).await {
            return Ok(latest.map_or(SimOutcome::CompletedNoEdit, SimOutcome::Replied));
        }
        let now = Instant::now();
        if now >= deadline {
            return Ok(SimOutcome::TimedOut);
        }
        tokio::time::sleep(POLL_INTERVAL.min(deadline - now)).await;
    }
}

/// The `agentos slack-sim` handler.
pub async fn slack_sim(opts: SlackSimOpts) -> Result<()> {
    let slack = SlackClient::new(SLACK_API_BASE, &opts.bot_token)?;

    // Connect to Valkey up front, before any Slack post. The Slack messages are
    // the irreversible side effect (an orphaned thread in a real channel), so a
    // misconfigured or unreachable Valkey must fail here, not after posting.
    let mut conn = connect(&opts.valkey_url).await?;

    // 1. Root message rendering the simulated user text, then 2. the threaded
    // placeholder the worker will edit in place.
    let thread_ts = slack
        .post_message(&opts.channel, &opts.text, None)
        .await
        .context("posting the simulated user message")?;
    let placeholder_ts = slack
        .post_message(&opts.channel, PLACEHOLDER_TEXT, Some(&thread_ts))
        .await
        .context("posting the placeholder reply")?;

    let link = match slack.permalink(&opts.channel, &thread_ts).await {
        Ok(url) => url,
        Err(_) => format!("{}/{}", opts.channel, thread_ts),
    };
    println!("thread   {link}");

    // 3. Enqueue the exact event the dispatcher would have produced.
    let event = QueuedSlackEvent::synthetic(
        &opts.channel,
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

    // 4. Wait for the worker to finalize (XACK), then read the final placeholder
    // text, or time out with diagnostics.
    println!(
        "waiting up to {}s for the worker to finalize the turn...",
        opts.timeout_secs
    );
    let outcome = wait_for_completion(
        &slack,
        &mut conn,
        &opts.stream,
        &stream_id,
        &opts.channel,
        &thread_ts,
        &placeholder_ts,
        PLACEHOLDER_TEXT,
        Duration::from_secs(opts.timeout_secs),
    )
    .await?;

    match outcome {
        SimOutcome::Replied(reply) => {
            println!("reply    {reply}");
            Ok(())
        }
        SimOutcome::CompletedNoEdit => {
            println!("the worker finished the turn but never edited the placeholder");
            Ok(())
        }
        SimOutcome::PollScopeMissing => {
            println!(
                "NOTE: the bot token lacks the channels:history scope, so live-polling \
                 the reply is unavailable until the Slack app is reinstalled with the \
                 updated manifest (apps/dispatcher/slack-app-manifest.yaml)."
            );
            println!("The worker still handled the turn in Slack. Watch the thread:");
            println!("thread   {link}");
            Ok(())
        }
        SimOutcome::TimedOut => {
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

    fn msg(ts: &str, text: &str) -> SlackMessage {
        SlackMessage {
            ts: ts.to_string(),
            text: text.to_string(),
        }
    }

    #[test]
    fn detects_the_placeholder_edit_by_ts() {
        let messages = vec![
            msg("root.1", "the user text"),
            msg("ph.1", "The actual answer from the worker."),
        ];
        let got = reply_text_if_changed(&messages, "ph.1", PLACEHOLDER_TEXT);
        assert_eq!(got.as_deref(), Some("The actual answer from the worker."));
    }

    #[test]
    fn unchanged_placeholder_is_still_pending() {
        let messages = vec![
            msg("root.1", "the user text"),
            msg("ph.1", PLACEHOLDER_TEXT),
        ];
        assert_eq!(
            reply_text_if_changed(&messages, "ph.1", PLACEHOLDER_TEXT),
            None
        );
    }

    #[test]
    fn missing_or_empty_placeholder_is_pending_not_a_false_positive() {
        // Placeholder not present yet.
        let messages = vec![msg("root.1", "the user text")];
        assert_eq!(
            reply_text_if_changed(&messages, "ph.1", PLACEHOLDER_TEXT),
            None
        );
        // Present but empty (e.g. a transient read): not a change.
        let messages = vec![msg("ph.1", "")];
        assert_eq!(
            reply_text_if_changed(&messages, "ph.1", PLACEHOLDER_TEXT),
            None
        );
    }

    #[test]
    fn missing_scope_slack_error_is_classified_recoverable() {
        let err: anyhow::Error = SlackApiError {
            method: "conversations.replies",
            code: "missing_scope".into(),
        }
        .into();
        assert!(is_missing_scope(&err));
    }

    #[test]
    fn other_errors_are_not_missing_scope() {
        let other_slack: anyhow::Error = SlackApiError {
            method: "conversations.replies",
            code: "channel_not_found".into(),
        }
        .into();
        assert!(!is_missing_scope(&other_slack));

        // A non-Slack error (e.g. a transport failure) is never missing_scope.
        assert!(!is_missing_scope(&anyhow::anyhow!("connection reset")));
    }
}
