"""End-to-end dispatch through Bolt's real Socket Mode handler, offline.

We drive ``SocketModeHandler.handle`` with a fake socket client and a mocked Web
API client (the only two things faked, per test discipline), and assert the full
lifecycle step: envelope -> ack -> in-thread placeholder -> XADD to real Valkey.
"""

import logging
from typing import Any
from unittest.mock import MagicMock

import redis
from agentos_dispatcher.app import build_app
from agentos_dispatcher.config import DispatcherConfig
from agentos_dispatcher.queue import QueuedSlackEvent
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_bolt.authorization import AuthorizeResult
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.web import WebClient

BOT_TS = "555.000"


def _authorize(**_kwargs: Any) -> AuthorizeResult:
    return AuthorizeResult(
        enterprise_id=None,
        team_id="T1",
        bot_token="xoxb-test",
        bot_id="B1",
        bot_user_id="U0BOT",
    )


class FakeSocketClient:
    """Captures the envelope acks Bolt sends back over the socket."""

    def __init__(self) -> None:
        self.logger = logging.getLogger("fake-socket")
        self.acked_envelope_ids: list[str] = []

    def send_socket_mode_response(self, response: Any) -> None:
        self.acked_envelope_ids.append(response.envelope_id)


def _build(config: DispatcherConfig, redis_client: redis.Redis) -> tuple[App, WebClient]:
    web_client = WebClient(token="xoxb-test")
    web_client.chat_postMessage = MagicMock(return_value={"ts": BOT_TS})  # type: ignore[method-assign]
    app = build_app(
        config,
        web_client=web_client,
        redis_client=redis_client,
        authorize=_authorize,
    )
    return app, web_client


def _drain(app: App) -> None:
    """Wait for Bolt's async listeners (run on a thread pool for fast ack) to finish.

    Production acks the envelope immediately and processes in the background; this
    drains that background work so assertions are deterministic.
    """
    app.listener_runner.listener_executor.shutdown(wait=True)


def _events_api_request(
    envelope_id: str,
    event_id: str,
    event: dict[str, Any],
) -> SocketModeRequest:
    return SocketModeRequest(
        type="events_api",
        envelope_id=envelope_id,
        payload={
            "type": "event_callback",
            "event_id": event_id,
            "team_id": "T1",
            "event": event,
        },
    )


def _mention_event(text: str = "hi there") -> dict[str, Any]:
    return {
        "type": "app_mention",
        "channel": "C123",
        "user": "U123",
        "text": text,
        "ts": "1700.0001",
    }


def test_envelope_acked_placeholder_posted_and_enqueued(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    app, web_client = _build(config, redis_client)
    handler = SocketModeHandler(app, app_token="xapp-test")
    sock = FakeSocketClient()

    handler.handle(sock, _events_api_request("env-1", "Ev-100", _mention_event()))

    # 1) The envelope was acked (fast-ack path), before background work completes.
    assert sock.acked_envelope_ids == ["env-1"]

    _drain(app)

    # 2) A placeholder was posted in-thread (root ts becomes the thread key).
    web_client.chat_postMessage.assert_called_once_with(
        channel="C123", thread_ts="1700.0001", text=config.placeholder_text
    )

    # 3) Exactly one job was enqueued, carrying the placeholder ts for the worker.
    assert redis_client.xlen(config.stream) == 1
    _, fields = redis_client.xrange(config.stream)[0]
    queued = QueuedSlackEvent.from_stream_fields(fields)
    assert queued.slack_event_id == "Ev-100"
    assert queued.channel == "C123"
    assert queued.user == "U123"
    assert queued.text == "hi there"
    assert queued.thread_ts == "1700.0001"
    assert queued.placeholder_ts == BOT_TS


def test_shimmer_sets_assistant_status_after_placeholder(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    shimmer_config = config.model_copy(update={"shimmer": True})
    app, web_client = _build(shimmer_config, redis_client)
    web_client.assistant_threads_setStatus = MagicMock()  # type: ignore[method-assign]
    handler = SocketModeHandler(app, app_token="xapp-test")
    sock = FakeSocketClient()

    handler.handle(sock, _events_api_request("env-1", "Ev-shim", _mention_event()))
    _drain(app)

    # The shimmer status is set on the same thread as the placeholder.
    web_client.assistant_threads_setStatus.assert_called_once_with(
        channel_id="C123", thread_ts="1700.0001", status=shimmer_config.placeholder_text
    )
    # And the normal placeholder + enqueue still happen.
    assert redis_client.xlen(config.stream) == 1


def test_duplicate_delivery_enqueues_exactly_once(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    app, web_client = _build(config, redis_client)
    handler = SocketModeHandler(app, app_token="xapp-test")
    sock = FakeSocketClient()

    # Same Slack event id delivered twice (a Slack retry).
    req_first = _events_api_request("env-1", "Ev-dup", _mention_event())
    req_retry = _events_api_request("env-2", "Ev-dup", _mention_event())
    handler.handle(sock, req_first)
    handler.handle(sock, req_retry)
    _drain(app)

    # Both envelopes are acked, but only one job is enqueued and one placeholder posted.
    assert sock.acked_envelope_ids == ["env-1", "env-2"]
    assert web_client.chat_postMessage.call_count == 1
    assert redis_client.xlen(config.stream) == 1


def test_message_in_dm_is_enqueued(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    app, _ = _build(config, redis_client)
    handler = SocketModeHandler(app, app_token="xapp-test")
    sock = FakeSocketClient()

    dm_event = {
        "type": "message",
        "channel_type": "im",
        "channel": "D1",
        "user": "U9",
        "text": "dm to the bot",
        "ts": "1800.0001",
    }
    handler.handle(sock, _events_api_request("env-1", "Ev-dm", dm_event))
    _drain(app)

    assert redis_client.xlen(config.stream) == 1


def test_message_in_channel_is_ignored(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    app, _ = _build(config, redis_client)
    handler = SocketModeHandler(app, app_token="xapp-test")
    sock = FakeSocketClient()

    channel_event = {
        "type": "message",
        "channel_type": "channel",
        "channel": "C1",
        "user": "U9",
        "text": "just chatting, not for the bot",
        "ts": "1900.0001",
    }
    handler.handle(sock, _events_api_request("env-1", "Ev-chan", channel_event))
    _drain(app)

    # Ordinary channel chatter is acked but never enqueued.
    assert sock.acked_envelope_ids == ["env-1"]
    assert redis_client.xlen(config.stream) == 0


def test_bot_authored_message_is_ignored(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    app, _ = _build(config, redis_client)
    handler = SocketModeHandler(app, app_token="xapp-test")
    sock = FakeSocketClient()

    # The dispatcher's own placeholder shows up as a bot message; it must not loop.
    bot_event = {
        "type": "message",
        "channel_type": "im",
        "channel": "D1",
        "bot_id": "B1",
        "text": "Working on it.",
        "ts": "2000.0001",
    }
    handler.handle(sock, _events_api_request("env-1", "Ev-bot", bot_event))
    _drain(app)

    assert redis_client.xlen(config.stream) == 0
