//! Shared machinery for the Slack-facing drivers (`chat` and `message`).
//!
//! Both mint the exact `QueuedTurn` the dispatcher would produce, `XADD` it onto
//! the real Valkey stream, and (on timeout) print the same stream diagnostics.
//! The queue seam is the frozen `QueuedTurn` contract promoted into
//! `packages/aci-protocol` (issue #7): the CLI uses the generated
//! `agentos_aci_protocol` types directly rather than hand-mirroring them. The
//! wire form is one `payload` field holding this model's JSON. The stream,
//! consumer-group, and payload-field transport literals come from that same
//! generated crate (issue #492), so a rename cannot drift this lane out of sync
//! with the dispatcher and worker.

use std::time::{SystemTime, UNIX_EPOCH};

use agentos_aci_protocol::{
    QueuedTurn, ReplyHandle, RUNS_STREAM_DEFAULT, STREAM_PAYLOAD_FIELD, WORKER_GROUP_DEFAULT,
};
use anyhow::{Context, Result};
use redis::aio::MultiplexedConnection;
use redis::streams::{StreamInfoGroupsReply, StreamPendingCountReply, StreamPendingReply};
use redis::AsyncCommands;
use time::format_description::well_known::Rfc3339;
use time::OffsetDateTime;
use uuid::Uuid;

pub const DEFAULT_STREAM: &str = RUNS_STREAM_DEFAULT;
pub const DEFAULT_VALKEY_URL: &str = "redis://:valkeypass@localhost:26379";
/// The worker's consumer group (AGENTOS_CONSUMER_GROUP default); used to detect
/// completion (the worker acks an entry only after the turn finalizes).
pub const WORKER_GROUP: &str = WORKER_GROUP_DEFAULT;

/// Prefix on the synthetic event id so dedupe can never collide with a real
/// Slack event id (which are `Ev...`, not `EvSIM-...`).
const EVENT_ID_PREFIX: &str = "EvSIM-";

/// Build a synthetic turn: a fresh `EvSIM-` id and the current UTC time, with the
/// given conversation/reply coordinates. Maps the Slack-facing drivers' inputs
/// onto the channel-neutral `QueuedTurn` (channel + placeholder live in the
/// `reply_handle`). ``endpoint`` is this turn's reply target (issue #19): the base
/// URL the worker delivers the reply through, so the CLI stub receives it without
/// re-pointing the worker's global setting. ``None`` uses the worker default.
pub fn synthetic_turn(
    channel: impl Into<String>,
    author: impl Into<String>,
    text: impl Into<String>,
    conversation_id: impl Into<String>,
    placeholder: impl Into<String>,
    endpoint: Option<String>,
) -> QueuedTurn {
    QueuedTurn {
        event_id: new_event_id(),
        conversation_id: conversation_id.into(),
        author: author.into(),
        text: text.into(),
        reply_handle: ReplyHandle {
            channel: channel.into(),
            placeholder: placeholder.into(),
            endpoint,
        },
        received_at: now_rfc3339(),
    }
}

/// The JSON blob stored under the stream's single `payload` field.
pub fn payload_json(turn: &QueuedTurn) -> Result<String> {
    serde_json::to_string(turn).context("serializing the queued turn")
}

/// A synthetic Slack event id with the `EvSIM-` prefix and a random suffix.
pub fn new_event_id() -> String {
    format!("{EVENT_ID_PREFIX}{}", Uuid::new_v4())
}

/// A synthetic Slack channel id; only needs to be internally consistent since
/// the CLI is both producer and (for `chat`) the Slack endpoint.
pub fn synthetic_channel() -> String {
    format!("C-SIM-{}", Uuid::new_v4().simple())
}

/// A distinct thread ts and placeholder ts in Slack's `<secs>.<micros>` shape,
/// built from one clock read so they share a second but never collide.
pub fn synthetic_thread_and_placeholder() -> (String, String) {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is after the epoch");
    let secs = now.as_secs();
    let base = now.subsec_micros();
    let ts = |offset: u32| format!("{secs}.{:06}", (base + offset) % 1_000_000);
    (ts(100), ts(200))
}

fn now_rfc3339() -> String {
    OffsetDateTime::now_utc()
        .format(&Rfc3339)
        .unwrap_or_else(|_| "1970-01-01T00:00:00Z".to_string())
}

pub async fn connect(url: &str) -> Result<MultiplexedConnection> {
    let client = redis::Client::open(url).context("opening the Valkey client")?;
    client
        .get_multiplexed_async_connection()
        .await
        .context("connecting to Valkey")
}

/// Append the event to the Stream under the frozen single-`payload` encoding;
/// returns the generated Stream entry id.
pub async fn xadd(
    conn: &mut MultiplexedConnection,
    stream: &str,
    turn: &QueuedTurn,
) -> Result<String> {
    let payload = payload_json(turn)?;
    let stream_id: String = redis::cmd("XADD")
        .arg(stream)
        .arg("*")
        .arg(STREAM_PAYLOAD_FIELD)
        .arg(payload)
        .query_async(conn)
        .await
        .with_context(|| format!("XADD onto {stream}"))?;
    Ok(stream_id)
}

/// Whether `group` has delivered `entry_id` and no longer holds it pending,
/// i.e. the worker consumed and acked it. The worker acks only after the turn
/// finalizes, so this is a real turn-completion signal, not a timing guess.
pub async fn entry_acked(
    conn: &mut MultiplexedConnection,
    stream: &str,
    group: &str,
    entry_id: &str,
) -> bool {
    let Some(last_delivered) = group_last_delivered(conn, stream, group).await else {
        return false;
    };
    if !id_ge(&last_delivered, entry_id) {
        return false;
    }
    !entry_pending(conn, stream, group, entry_id).await
}

async fn group_last_delivered(
    conn: &mut MultiplexedConnection,
    stream: &str,
    group_name: &str,
) -> Option<String> {
    let reply: StreamInfoGroupsReply = conn.xinfo_groups(stream).await.ok()?;
    for g in &reply.groups {
        if g.name == group_name {
            return Some(g.last_delivered_id.clone());
        }
    }
    None
}

async fn entry_pending(
    conn: &mut MultiplexedConnection,
    stream: &str,
    group: &str,
    entry_id: &str,
) -> bool {
    let reply: redis::RedisResult<StreamPendingCountReply> = conn
        .xpending_count(stream, group, entry_id, entry_id, 1)
        .await;
    matches!(reply, Ok(r) if !r.ids.is_empty())
}

fn parse_stream_id(id: &str) -> Option<(u64, u64)> {
    let (ms, seq) = id.split_once('-')?;
    Some((ms.parse().ok()?, seq.parse().ok()?))
}

/// Stream-id ordering: `a >= b` on the `<ms>-<seq>` pair. Unparseable ids
/// compare false (treated as "not yet delivered").
fn id_ge(a: &str, b: &str) -> bool {
    match (parse_stream_id(a), parse_stream_id(b)) {
        (Some(x), Some(y)) => x >= y,
        _ => false,
    }
}

/// Best-effort stream state after a timeout: length, our entry, and every
/// consumer group's progress and pending list so the operator can see whether
/// the worker consumed the entry.
pub async fn diagnostics(
    conn: &mut MultiplexedConnection,
    stream: &str,
    stream_id: &str,
) -> String {
    let mut lines = vec![format!("  stream {stream}, our entry {stream_id}")];

    match redis::cmd("XLEN")
        .arg(stream)
        .query_async::<i64>(conn)
        .await
    {
        Ok(len) => lines.push(format!("  XLEN {len}")),
        Err(err) => lines.push(format!("  XLEN unavailable: {err}")),
    }

    match conn.xinfo_groups::<_, StreamInfoGroupsReply>(stream).await {
        Ok(reply) if reply.groups.is_empty() => {
            lines.push("  no consumer groups: the worker is not consuming this stream".into());
        }
        Ok(reply) => {
            for g in &reply.groups {
                lines.push(format!("  group {}", render_group(g)));
                lines.push(format!(
                    "  XPENDING {}: {}",
                    g.name,
                    xpending(conn, stream, &g.name).await
                ));
            }
        }
        Err(err) => lines.push(format!("  XINFO GROUPS unavailable: {err}")),
    }

    lines.join("\n")
}

/// Render a consumer group's typed XINFO fields as space-joined `key=value`
/// pairs for the diagnostics printout.
fn render_group(g: &redis::streams::StreamInfoGroup) -> String {
    format!(
        "name={} consumers={} pending={} last-delivered-id={} entries-read={:?} lag={:?}",
        g.name, g.consumers, g.pending, g.last_delivered_id, g.entries_read, g.lag
    )
}

async fn xpending(conn: &mut MultiplexedConnection, stream: &str, group: &str) -> String {
    match conn
        .xpending::<_, _, StreamPendingReply>(stream, group)
        .await
    {
        Ok(reply) => render_pending(&reply),
        Err(err) => format!("unavailable: {err}"),
    }
}

/// Render a summary XPENDING reply for the diagnostics printout.
fn render_pending(reply: &StreamPendingReply) -> String {
    match reply {
        StreamPendingReply::Empty => "empty".to_string(),
        StreamPendingReply::Data(d) => {
            let consumers: Vec<String> = d
                .consumers
                .iter()
                .map(|c| format!("{}:{}", c.name, c.pending))
                .collect();
            format!(
                "count={} start-id={} end-id={} consumers=[{}]",
                d.count,
                d.start_id,
                d.end_id,
                consumers.join(",")
            )
        }
        // `StreamPendingReply` is `#[non_exhaustive]` (redis 1.x); render any
        // future variant as unknown rather than failing to compile.
        _ => "unknown".to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn event_id_has_sim_prefix_and_is_unique() {
        let a = new_event_id();
        let b = new_event_id();
        assert!(a.starts_with("EvSIM-"), "unexpected id: {a}");
        assert_ne!(a, b, "event ids must be unique");
        Uuid::parse_str(a.trim_start_matches("EvSIM-")).expect("suffix is a uuid");
    }

    #[test]
    fn queued_turn_matches_cross_language_golden() {
        // The same committed wire fixture the Python producer (apps/dispatcher)
        // round-trips: the generated QueuedTurn must deserialize it and
        // re-serialize to identical bytes. This pins the frozen QueuedTurn seam
        // across the Python producer and this Rust consumer (issue #7).
        let raw =
            include_str!("../../packages/aci-protocol/schema/queued-turn.fixture.json").trim_end();
        let turn: QueuedTurn = serde_json::from_str(raw).expect("golden deserializes");
        assert_eq!(turn.event_id, "Ev0GOLDEN0001");
        assert_eq!(turn.received_at, "2026-07-05T00:00:00+00:00");
        assert_eq!(
            payload_json(&turn).unwrap(),
            raw,
            "Rust wire bytes drifted from golden"
        );
    }

    #[test]
    fn payload_json_carries_the_exact_seam_field_names() {
        let turn = synthetic_turn(
            "C-SIM-x",
            "U-agentos-chat",
            "hello",
            "1720000000.000100",
            "1720000000.000200",
            None,
        );
        let json = payload_json(&turn).unwrap();
        let value: serde_json::Value = serde_json::from_str(&json).unwrap();
        let object = value.as_object().unwrap();

        let mut keys: Vec<&str> = object.keys().map(String::as_str).collect();
        keys.sort_unstable();
        assert_eq!(
            keys,
            vec![
                "author",
                "conversation_id",
                "event_id",
                "received_at",
                "reply_handle",
                "text",
            ]
        );
        // channel and placeholder are nested in the channel-neutral reply_handle.
        assert_eq!(object["reply_handle"]["channel"], "C-SIM-x");
        assert_eq!(object["reply_handle"]["placeholder"], "1720000000.000200");
        assert_eq!(object["conversation_id"], "1720000000.000100");
        // No per-turn endpoint set: it rides the wire as null (worker default).
        assert!(object["reply_handle"]["endpoint"].is_null());
    }

    #[test]
    fn synthetic_turn_stamps_the_per_turn_reply_endpoint() {
        // Issue #19: a CLI-minted turn carries its own reply endpoint so the worker
        // posts back to this stub without re-pointing its global setting.
        let turn = synthetic_turn(
            "C-SIM-x",
            "U-agentos-chat",
            "hi",
            "1.1",
            "1.2",
            Some("http://10.1.2.3:8155/api/".to_string()),
        );
        assert_eq!(
            turn.reply_handle.endpoint.as_deref(),
            Some("http://10.1.2.3:8155/api/")
        );
        let json = payload_json(&turn).unwrap();
        let value: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(
            value["reply_handle"]["endpoint"],
            "http://10.1.2.3:8155/api/"
        );
    }

    #[test]
    fn synthetic_ids_are_distinct_and_slack_shaped() {
        let (thread_ts, placeholder_ts) = synthetic_thread_and_placeholder();
        assert_ne!(thread_ts, placeholder_ts);
        for ts in [&thread_ts, &placeholder_ts] {
            let (secs, micros) = ts.split_once('.').expect("dot separator");
            assert!(secs.parse::<u64>().is_ok(), "secs: {ts}");
            assert_eq!(micros.len(), 6, "micros width: {ts}");
        }
        assert!(synthetic_channel().starts_with("C-SIM-"));
    }

    #[test]
    fn stream_id_ordering_compares_ms_then_seq() {
        assert!(id_ge("5-0", "5-0"));
        assert!(id_ge("5-1", "5-0"));
        assert!(id_ge("6-0", "5-9"));
        assert!(!id_ge("5-0", "5-1"));
        assert!(!id_ge("4-9", "5-0"));
        // Unparseable compares false (not-yet-delivered).
        assert!(!id_ge("0-0", "not-an-id"));
    }
}
