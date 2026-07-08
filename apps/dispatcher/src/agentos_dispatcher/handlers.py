"""Slack event handling: the fast-ack path that posts a placeholder and enqueues.

The Socket Mode transport acks the envelope in under three seconds on its own
(the handler sends the ack before dispatching to these listeners). These handlers
own the rest of the lifecycle step: dedupe the delivery, post an in-thread
placeholder reply, and enqueue the normalized job for the worker.

Two event types feed the same processing path:
  - ``app_mention``: the bot was @-mentioned in a channel; always process.
  - ``message``: only direct messages to the bot (``channel_type == "im"``) are
    processed, so ordinary channel chatter is not enqueued.

We use the dispatcher's own ``WebClient`` (built from the bot token) rather than
Bolt's per-request injected client so the Web API surface is a single, mockable
seam. Routing, retries, and run orchestration are the worker's job (F1), not the
dispatcher's.
"""

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from slack_bolt import App
from slack_sdk.web import WebClient

from .config import DispatcherConfig
from .queue import QueuedSlackEvent, claim_event, enqueue

if TYPE_CHECKING:
    from redis import Redis

Clock = Callable[[], str]


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def is_actionable(event: dict[str, Any]) -> bool:
    """False for events the dispatcher must ignore to avoid loops and noise.

    Bot-authored messages (including the dispatcher's own placeholder) and
    subtyped messages (edits, joins, deletions) are not user requests.
    """
    if event.get("bot_id"):
        return False
    if event.get("subtype"):
        return False
    return True


def process_event(
    *,
    body: dict[str, Any],
    event: dict[str, Any],
    web_client: WebClient,
    redis_client: "Redis",
    config: DispatcherConfig,
    clock: Clock = _utc_now_iso,
    logger: logging.Logger | None = None,
) -> str | None:
    """Dedupe, post the placeholder, and enqueue one Slack event.

    Returns the Valkey Stream id when a job was enqueued, or None when the event
    was skipped (non-actionable, or a duplicate delivery already claimed).
    """
    log = logger or logging.getLogger(__name__)

    if not is_actionable(event):
        return None

    slack_event_id = body["event_id"]

    if not claim_event(redis_client, config, slack_event_id):
        log.info("duplicate slack event %s, skipping", slack_event_id)
        return None

    # Reply in-thread: for a root message the thread key is its own ts.
    thread_ts = event.get("thread_ts") or event["ts"]
    channel = event["channel"]

    placeholder = web_client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=config.placeholder_text,
    )
    placeholder_ts = placeholder["ts"]

    if config.shimmer:
        # Native Slack "shimmer": set the assistant-thread status so the app name
        # shimmers while we work. Best-effort -- a workspace without the assistant
        # feature just skips it; the worker clears the status when the turn ends.
        try:
            web_client.assistant_threads_setStatus(
                channel_id=channel, thread_ts=thread_ts, status=config.placeholder_text
            )
        except Exception as exc:  # noqa: BLE001 -- shimmer is best-effort, never fatal
            log.debug("assistant setStatus skipped for %s: %s", slack_event_id, exc)

    queued = QueuedSlackEvent(
        slack_event_id=slack_event_id,
        thread_ts=thread_ts,
        channel=channel,
        user=event.get("user", ""),
        text=event.get("text", ""),
        placeholder_ts=placeholder_ts,
        received_at=clock(),
    )
    stream_id = enqueue(redis_client, config, queued)
    log.info("enqueued slack event %s as stream entry %s", slack_event_id, stream_id)
    return stream_id


def action_command(action: dict[str, Any]) -> str:
    """The command a clicked Block Kit action carries: its ``value`` if set, else
    its ``action_id`` (the ss-template convention where a button's id is the
    command it runs)."""
    value = action.get("value")
    return str(value) if value else str(action.get("action_id", ""))


def process_action(
    *,
    body: dict[str, Any],
    web_client: WebClient,
    redis_client: "Redis",
    config: DispatcherConfig,
    clock: Clock = _utc_now_iso,
    logger: logging.Logger | None = None,
) -> str | None:
    """Normalize a Block Kit button click into a turn: dedupe, post an in-thread
    placeholder, and enqueue a ``QueuedSlackEvent`` whose text is the button's
    command. The worker answers it exactly as if the user had typed that command.

    Same four steps as ``process_event`` (ack is Bolt's, before this runs); no
    decision about *how* the turn is answered lives here -- that is the worker's.
    """
    log = logger or logging.getLogger(__name__)

    actions = body.get("actions") or []
    if not actions:
        return None
    command = action_command(actions[0])
    if not command:
        return None

    # A click carries no Slack event_id, so synthesize a stable idempotency key
    # from the interaction; a re-delivered click cannot enqueue (or post a second
    # placeholder) twice, same as the event dedupe.
    interaction = body.get("trigger_id") or (
        f"{actions[0].get('action_ts', '')}-{actions[0].get('action_id', '')}"
    )
    slack_event_id = f"action-{interaction}"
    if not claim_event(redis_client, config, slack_event_id):
        log.info("duplicate block action %s, skipping", slack_event_id)
        return None

    channel = body["channel"]["id"]
    message = body.get("message") or {}
    # Reply in the clicked message's thread (its thread_ts, or its own ts if root).
    thread_ts = message.get("thread_ts") or message.get("ts") or ""
    user = (body.get("user") or {}).get("id", "")

    placeholder = web_client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=config.placeholder_text,
    )
    queued = QueuedSlackEvent(
        slack_event_id=slack_event_id,
        thread_ts=thread_ts,
        channel=channel,
        user=user,
        text=command,
        placeholder_ts=placeholder["ts"],
        received_at=clock(),
    )
    stream_id = enqueue(redis_client, config, queued)
    log.info("enqueued block action %s as stream entry %s", slack_event_id, stream_id)
    return stream_id


def register_handlers(
    app: App,
    *,
    web_client: WebClient,
    redis_client: "Redis",
    config: DispatcherConfig,
    clock: Clock = _utc_now_iso,
    logger: logging.Logger | None = None,
) -> None:
    """Wire the app_mention, (direct-message) message, and block-action listeners."""

    @app.event("app_mention")
    def _on_app_mention(body: dict[str, Any], event: dict[str, Any]) -> None:
        process_event(
            body=body,
            event=event,
            web_client=web_client,
            redis_client=redis_client,
            config=config,
            clock=clock,
            logger=logger,
        )

    @app.event("message")
    def _on_message(body: dict[str, Any], event: dict[str, Any]) -> None:
        if event.get("channel_type") != "im":
            return
        process_event(
            body=body,
            event=event,
            web_client=web_client,
            redis_client=redis_client,
            config=config,
            clock=clock,
            logger=logger,
        )

    # Any Block Kit button click (a reply's action) becomes a turn. The catch-all
    # matches every action_id; ack first (Bolt's 3s budget), then normalize+enqueue.
    @app.action(re.compile(r".+"))
    def _on_action(ack: Callable[..., None], body: dict[str, Any]) -> None:
        ack()
        process_action(
            body=body,
            web_client=web_client,
            redis_client=redis_client,
            config=config,
            clock=clock,
            logger=logger,
        )
