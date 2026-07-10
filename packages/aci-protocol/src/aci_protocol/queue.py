"""The dispatcher-to-worker queue job, promoted into the frozen contract (#7).

`QueuedSlackEvent` is the normalized inbound event the dispatcher enqueues on the
Valkey stream and the worker consumes. It was hand-mirrored in the dispatcher
(Python) and the CLI (Rust); it now lives here as the single tri-language source
of truth, generated into Rust and TypeScript like the rest of the contract.

Two deliberate differences from the NDJSON runner frames in this package:

- **No on-wire version, not gated by PROTOCOL_VERSION.** This is a Valkey stream
  payload, not an ACI runner frame. It carries no `version` field, and promoting
  it does NOT bump PROTOCOL_VERSION (that constant versions the runner<->worker
  NDJSON wire; bumping it for a queue-payload change would needlessly reject
  in-flight runner frames). See the ADR for #7.
- **Strict (`extra="forbid"`).** It adopts the package convention (aci-protocol
  rejects unknown fields; see packages/CLAUDE.md), consistent with the generated
  Rust `deny_unknown_fields`. The promotion is wire-invisible: the payload still
  carries exactly these seven fields. When #7 PR-B renames them, the rollout
  carries backward-compat aliases so a mixed-version fleet keeps parsing.

The Slack-shaped field names are preserved verbatim here; making them
channel-neutral is a separate follow-up (#7 PR-B).
"""

from pydantic import BaseModel, ConfigDict


class QueuedSlackEvent(BaseModel):
    """A normalized Slack event ready for the worker to route and run.

    Fields:
        slack_event_id: Slack's per-delivery event id; the idempotency key.
        thread_ts: the canonical thread key (the root message ts of the thread).
        channel: Slack channel id the message arrived in.
        user: Slack user id that authored the message.
        text: the message text.
        placeholder_ts: ts of the placeholder reply the dispatcher already posted;
            the worker edits this message in place with the real response.
        received_at: ISO-8601 UTC timestamp of when the dispatcher received it.
    """

    model_config = ConfigDict(extra="forbid")

    slack_event_id: str
    thread_ts: str
    channel: str
    user: str
    text: str
    placeholder_ts: str
    received_at: str
