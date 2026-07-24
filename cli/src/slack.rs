//! A minimal Slack Web API client for the connected-transport `message` path
//! (#770/ADR-0078).
//!
//! `local`/`cluster message` normally mint a throwaway in-process reply stub.
//! When a real workspace is connected, we instead post a real placeholder
//! message to the channel over the workspace bot token and enqueue the turn
//! against its real `ts`, so the worker edits that message in place and the
//! approval card threads under it -- the card and the resumed reply ride the
//! connected transport with no stub. This module owns just that one outbound
//! call.

use anyhow::{Context, Result};

/// The Slack Web API base. `SLACK_API_BASE_URL` overrides it (a test/stub base,
/// the same env the worker's own sink honours), else real Slack.
fn api_base() -> String {
    std::env::var("SLACK_API_BASE_URL")
        .ok()
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "https://slack.com/api".to_string())
}

/// Post `text` to `channel` as the bot, returning the created message `ts` (the
/// placeholder the worker then edits in place). The token is sent only in the
/// Authorization header; it is never logged.
pub async fn post_placeholder(bot_token: &str, channel: &str, text: &str) -> Result<String> {
    let base = api_base();
    let resp = reqwest::Client::new()
        .post(format!("{base}/chat.postMessage"))
        .bearer_auth(bot_token)
        .json(&serde_json::json!({ "channel": channel, "text": text }))
        .send()
        .await
        .context("posting the approval-turn placeholder to Slack")?
        .json::<serde_json::Value>()
        .await
        .context("decoding the Slack chat.postMessage response")?;
    parse_ts(&resp)
}

/// Extract the created message `ts` from a `chat.postMessage` response, turning
/// an `{"ok": false, "error": ...}` body into an actionable error. Pure, so it
/// is unit tested without a network round trip.
fn parse_ts(resp: &serde_json::Value) -> Result<String> {
    if resp.get("ok").and_then(serde_json::Value::as_bool) != Some(true) {
        let err = resp
            .get("error")
            .and_then(serde_json::Value::as_str)
            .unwrap_or("unknown error");
        anyhow::bail!("Slack chat.postMessage failed: {err}");
    }
    resp.get("ts")
        .and_then(serde_json::Value::as_str)
        .map(str::to_string)
        .context("Slack chat.postMessage returned ok but no message ts")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn parse_ts_returns_the_ts_on_ok() {
        assert_eq!(
            parse_ts(&json!({"ok": true, "ts": "1717171717.001900"})).unwrap(),
            "1717171717.001900"
        );
    }

    #[test]
    fn parse_ts_surfaces_the_slack_error_on_not_ok() {
        let err = parse_ts(&json!({"ok": false, "error": "channel_not_found"})).unwrap_err();
        assert!(err.to_string().contains("channel_not_found"), "{err}");
    }

    #[test]
    fn parse_ts_errors_when_ok_but_no_ts() {
        assert!(parse_ts(&json!({"ok": true})).is_err());
    }

    #[test]
    fn api_base_defaults_to_real_slack_and_honours_the_override() {
        // No override -> real Slack. (Set/removed in-process; keep the assertions
        // tolerant of a pre-set env by asserting the shape, not a fixed value.)
        std::env::remove_var("SLACK_API_BASE_URL");
        assert_eq!(api_base(), "https://slack.com/api");
        std::env::set_var("SLACK_API_BASE_URL", "http://stub:9/api");
        assert_eq!(api_base(), "http://stub:9/api");
        std::env::remove_var("SLACK_API_BASE_URL");
    }
}
