"""The queued turn payload: the normalized inbound job an ingress adapter
enqueues and the worker consumes.

This is the channel-neutral promotion of the dispatcher's former
``QueuedSlackEvent`` (issue #7). It lives here, in the frozen ACI package, so the
contract is shared across all three languages (Pydantic source of truth, with
generated TypeScript and Rust derived from the committed JSON Schema) and guarded
by the schema-compat gate, rather than hand-mirrored between the Python producer
and the Rust CLI.

The field names are channel-agnostic so a second ingress adapter (not just Slack)
can produce and route the same payload:

    event_id        idempotency key for the delivery
    conversation_id the conversation/thread key routing keeps one live session per
    author          who authored the message
    text            the message text
    reply_handle    where the reply is delivered (see ``ReplyHandle``)
    received_at     ISO-8601 UTC timestamp of when the adapter received it

For the Slack adapter today, ``event_id`` is the Slack event id, ``conversation_id``
is the thread ts, ``author`` is the Slack user id, and ``reply_handle`` carries the
Slack channel plus the placeholder message ts. The Valkey Stream wire encoding (a
single ``payload`` field holding this model's JSON) is a transport detail and
stays outside this package, in the dispatcher's queue module.
"""

from pydantic import BaseModel, ConfigDict

_STRICT = ConfigDict(extra="forbid")


class ReplyHandle(BaseModel):
    """Channel-neutral coordinates for where a turn's reply is delivered.

    The reply model is edit-in-place: the ingress adapter pre-posts a placeholder
    message and the worker edits it as the answer streams. For the Slack adapter
    today, ``channel`` is the Slack channel id (also the key the deployment
    binding matches an agent on) and ``placeholder`` is the ts of the pre-posted
    placeholder message. Issue #19 extends this with a per-turn reply endpoint so
    two ingress paths can coexist on one worker.
    """

    model_config = _STRICT

    channel: str
    placeholder: str


class QueuedTurn(BaseModel):
    """A normalized inbound turn ready for the worker to route and run.

    The channel-neutral promotion of the dispatcher's former ``QueuedSlackEvent``.
    The Valkey Stream carries this model as a single ``payload`` JSON field; the
    stream-encoding helpers live with the producer (the dispatcher), not on this
    frozen model, so the contract stays transport-agnostic.
    """

    model_config = _STRICT

    event_id: str
    conversation_id: str
    author: str
    text: str
    reply_handle: ReplyHandle
    received_at: str
