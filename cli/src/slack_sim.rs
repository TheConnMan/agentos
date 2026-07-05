//! `agentos slack-sim`: the real-Slack egress leg without Socket Mode.
//!
//! It posts a synthetic conversation in a real Slack channel as the bot (a root
//! message rendering the simulated user text, then a threaded placeholder),
//! enqueues the exact `QueuedSlackEvent` the dispatcher would produce onto the
//! real Valkey stream, and polls `conversations.replies` until the worker edits
//! the placeholder in place. On success it prints the reply and exits 0; on
//! timeout it prints stream diagnostics and exits nonzero.
//!
//! This is the real-Slack rung of the ladder; the no-Slack rung is `chat`, which
//! makes the CLI itself the Slack service. Both share the frozen queue seam via
//! the `queue` module.

use std::time::{Duration, Instant};

use anyhow::{bail, Context, Result};
use serde::Deserialize;

use crate::queue::{self, connect, diagnostics, xadd, QueuedSlackEvent};

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
            bail!(
                "conversations.replies failed: {}",
                resp.error.unwrap_or_else(|| "unknown error".into())
            );
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

/// Poll `conversations.replies` until the placeholder text changes or the
/// deadline passes. `Ok(Some(text))` is the worker's reply; `Ok(None)` is a
/// timeout.
async fn poll_for_reply(
    slack: &SlackClient,
    channel: &str,
    thread_ts: &str,
    placeholder_ts: &str,
    original: &str,
    timeout: Duration,
) -> Result<Option<String>> {
    let deadline = Instant::now() + timeout;
    loop {
        let messages = slack.conversations_replies(channel, thread_ts).await?;
        if let Some(text) = reply_text_if_changed(&messages, placeholder_ts, original) {
            return Ok(Some(text));
        }
        let now = Instant::now();
        if now >= deadline {
            return Ok(None);
        }
        let remaining = deadline - now;
        tokio::time::sleep(POLL_INTERVAL.min(remaining)).await;
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

    // 4. Poll until the worker edits the placeholder, or time out with
    // diagnostics.
    println!(
        "waiting up to {}s for the worker to reply...",
        opts.timeout_secs
    );
    let outcome = poll_for_reply(
        &slack,
        &opts.channel,
        &thread_ts,
        &placeholder_ts,
        PLACEHOLDER_TEXT,
        Duration::from_secs(opts.timeout_secs),
    )
    .await?;

    match outcome {
        Some(reply) => {
            println!("reply    {reply}");
            Ok(())
        }
        None => {
            println!(
                "TIMEOUT: no reply after {}s. Stream diagnostics:",
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
}
