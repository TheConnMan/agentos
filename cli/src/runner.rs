//! HTTP client for the runner's ACI channel.
//!
//! Speaks the frozen contract only: inbound frames are the generated
//! `InboundMessage`, the `/v1/event` response is an NDJSON stream of
//! `OutboundEvent` frames (version-enforced at deserialization).

use std::time::Duration;

use agentos_aci_protocol::{EventType, InboundMessage, OutboundEvent};
use anyhow::{bail, Context, Result};
use futures_util::StreamExt;

use crate::ndjson::{parse_outbound, LineSplitter};

pub struct RunnerClient {
    base_url: String,
    http: reqwest::Client,
}

impl RunnerClient {
    pub fn new(base_url: &str) -> Result<Self> {
        let http = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(5))
            .build()
            .context("building HTTP client")?;
        Ok(Self {
            base_url: base_url.trim_end_matches('/').to_string(),
            http,
        })
    }

    pub async fn healthy(&self) -> bool {
        matches!(
            self.http
                .get(format!("{}/healthz", self.base_url))
                .send()
                .await,
            Ok(resp) if resp.status().is_success()
        )
    }

    /// Poll `/healthz` until the runner answers or the deadline passes.
    pub async fn wait_healthy(&self, deadline: Duration) -> Result<()> {
        let start = std::time::Instant::now();
        while start.elapsed() < deadline {
            if self.healthy().await {
                return Ok(());
            }
            tokio::time::sleep(Duration::from_millis(500)).await;
        }
        bail!(
            "runner at {} did not become healthy within {:?}",
            self.base_url,
            deadline
        )
    }

    pub async fn status(&self) -> Result<serde_json::Value> {
        let resp = self
            .http
            .get(format!("{}/status", self.base_url))
            .send()
            .await
            .with_context(|| format!("GET {}/status", self.base_url))?;
        if !resp.status().is_success() {
            bail!("GET /status returned {}", resp.status());
        }
        resp.json().await.context("decoding /status body")
    }

    /// Open a turn: POST the event frame, stream back the outbound events.
    ///
    /// `on_event` fires per frame as it arrives (live streaming to the
    /// terminal); the full ordered list is returned for callers that assert on
    /// the turn (evals). The turn must terminate in a `final` frame.
    pub async fn send_event(
        &self,
        event_type: EventType,
        text: &str,
        user: &str,
        mut on_event: impl FnMut(&OutboundEvent),
    ) -> Result<Vec<OutboundEvent>> {
        let frame = InboundMessage::Event {
            r#type: event_type,
            text: text.to_string(),
            user: user.to_string(),
            ts: slack_ts(),
        };
        let resp = self
            .http
            .post(format!("{}/v1/event", self.base_url))
            .json(&frame)
            .send()
            .await
            .with_context(|| format!("POST {}/v1/event", self.base_url))?;
        if !resp.status().is_success() {
            let status = resp.status();
            let body = resp.text().await.unwrap_or_default();
            bail!("POST /v1/event returned {status}: {}", body.trim());
        }

        let mut events = Vec::new();
        let mut splitter = LineSplitter::default();
        let mut stream = resp.bytes_stream();
        while let Some(chunk) = stream.next().await {
            let chunk = chunk.context("reading NDJSON stream")?;
            for line in splitter.push(&chunk) {
                if line.trim().is_empty() {
                    continue;
                }
                let event = parse_outbound(&line)?;
                on_event(&event);
                events.push(event);
            }
        }
        if let Some(tail) = splitter.finish() {
            if !tail.trim().is_empty() {
                let event = parse_outbound(&tail)?;
                on_event(&event);
                events.push(event);
            }
        }

        if !matches!(events.last(), Some(OutboundEvent::Final { .. })) {
            bail!(
                "stream ended without a final frame ({} events)",
                events.len()
            );
        }
        Ok(events)
    }
}

/// A Slack-style event timestamp: `<unix seconds>.<microseconds>`.
fn slack_ts() -> String {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("system clock is after the epoch");
    format!("{}.{:06}", now.as_secs(), now.subsec_micros())
}

#[cfg(test)]
mod tests {
    use super::slack_ts;

    #[test]
    fn slack_ts_has_the_wire_shape() {
        let ts = slack_ts();
        let (secs, micros) = ts.split_once('.').expect("dot separator");
        assert!(secs.parse::<u64>().is_ok());
        assert_eq!(micros.len(), 6);
        assert!(micros.parse::<u32>().is_ok());
    }
}
