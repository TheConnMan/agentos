"""Click-to-resolve through the real Bolt Socket Mode handler, offline (#246).

Same discipline as test_dispatch.py: the envelope is driven through Bolt's real
``SocketModeHandler``; only the socket, the Web API client, and the platform
API (a scripted ``ApprovalResolveClient`` stand-in) are faked. Asserts the
acceptance behaviors: an authorized click resolves and stamps the card, a
non-approver gets the ephemeral rejection, a claim-race loser gets "already
resolved by X", and the ordinary-button catch-all never double-handles an
approval click.
"""

import logging
from typing import Any
from unittest.mock import MagicMock

import redis
from agentos_dispatcher.app import build_app
from agentos_dispatcher.approval_actions import (
    APPROVE_ACTION_ID,
    REJECT_ACTION_ID,
    ResolveOutcome,
)
from agentos_dispatcher.config import DispatcherConfig
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_bolt.authorization import AuthorizeResult
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.web import WebClient

APPROVAL_ID = "9a1e8a10-0000-0000-0000-000000000246"

_CARD_MESSAGE = {
    "ts": "1700.0042",
    "thread_ts": "1700.0001",
    "blocks": [
        {"type": "header", "text": {"type": "plain_text", "text": "Approval required"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "Discount for ACME"}},
        {"type": "actions", "elements": []},
    ],
}


def _authorize(**_kwargs: Any) -> AuthorizeResult:
    return AuthorizeResult(
        enterprise_id=None,
        team_id="T1",
        bot_token="xoxb-test",
        bot_id="B1",
        bot_user_id="U0BOT",
    )


class FakeSocketClient:
    def __init__(self) -> None:
        self.logger = logging.getLogger("fake-socket")
        self.acked_envelope_ids: list[str] = []

    def send_socket_mode_response(self, response: Any) -> None:
        self.acked_envelope_ids.append(response.envelope_id)


class ScriptedResolver:
    """Stands in for the platform API: returns a scripted outcome per call."""

    def __init__(self, outcome: ResolveOutcome) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, str]] = []

    def resolve(
        self, approval_id: str, *, decision: str, resolved_by: str, actor_channel: str
    ) -> ResolveOutcome:
        self.calls.append(
            {
                "approval_id": approval_id,
                "decision": decision,
                "resolved_by": resolved_by,
                "actor_channel": actor_channel,
            }
        )
        return self.outcome


def _build(
    config: DispatcherConfig, redis_client: redis.Redis, resolver: ScriptedResolver
) -> tuple[App, WebClient]:
    web_client = WebClient(token="xoxb-test")
    web_client.chat_postMessage = MagicMock(return_value={"ts": "555.000"})  # type: ignore[method-assign]
    web_client.chat_update = MagicMock(return_value={"ok": True})  # type: ignore[method-assign]
    web_client.chat_postEphemeral = MagicMock(return_value={"ok": True})  # type: ignore[method-assign]
    app = build_app(
        config,
        web_client=web_client,
        redis_client=redis_client,
        authorize=_authorize,
        resolver=resolver,
    )
    return app, web_client


def _drain(app: App) -> None:
    app.listener_runner.listener_executor.shutdown(wait=True)


def _approval_click(
    envelope_id: str, *, action_id: str, user: str = "U_MANAGER"
) -> SocketModeRequest:
    return SocketModeRequest(
        type="interactive",
        envelope_id=envelope_id,
        payload={
            "type": "block_actions",
            "trigger_id": f"trig-{envelope_id}",
            "team": {"id": "T1"},
            "user": {"id": user},
            "api_app_id": "A1",
            "token": "verif",
            "container": {"type": "message", "message_ts": _CARD_MESSAGE["ts"]},
            "channel": {"id": "C_MGRS"},
            "message": _CARD_MESSAGE,
            "actions": [
                {
                    "type": "button",
                    "action_id": action_id,
                    "action_ts": "2.0",
                    "value": APPROVAL_ID,
                }
            ],
        },
    )


def test_authorized_click_resolves_and_stamps_the_card(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    resolver = ScriptedResolver(
        ResolveOutcome(status_code=200, resolved_by="U_MANAGER", decision="approved")
    )
    app, web_client = _build(config, redis_client, resolver)
    handler = SocketModeHandler(app, app_token="xapp-test")
    sock = FakeSocketClient()

    handler.handle(sock, _approval_click("env-a1", action_id=APPROVE_ACTION_ID))
    assert sock.acked_envelope_ids == ["env-a1"]
    _drain(app)

    # The API was asked to resolve with the clicker's identity and channel
    # (the membership evidence the server-side authorizer checks).
    assert resolver.calls == [
        {
            "approval_id": APPROVAL_ID,
            "decision": "approved",
            "resolved_by": "U_MANAGER",
            "actor_channel": "C_MGRS",
        }
    ]

    # The card was stamped in place: buttons gone, verdict context appended.
    web_client.chat_update.assert_called_once()
    kwargs = web_client.chat_update.call_args.kwargs
    assert kwargs["channel"] == "C_MGRS" and kwargs["ts"] == _CARD_MESSAGE["ts"]
    assert all(b["type"] != "actions" for b in kwargs["blocks"])
    assert "Approved by <@U_MANAGER>" in kwargs["text"]

    # No turn was enqueued and no placeholder posted: the catch-all skipped it.
    web_client.chat_postMessage.assert_not_called()
    web_client.chat_postEphemeral.assert_not_called()


def test_reject_button_resolves_with_rejected_decision(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    resolver = ScriptedResolver(
        ResolveOutcome(status_code=200, resolved_by="U_MANAGER", decision="rejected")
    )
    app, web_client = _build(config, redis_client, resolver)
    handler = SocketModeHandler(app, app_token="xapp-test")

    handler.handle(FakeSocketClient(), _approval_click("env-r1", action_id=REJECT_ACTION_ID))
    _drain(app)

    assert resolver.calls[0]["decision"] == "rejected"
    assert "Rejected by <@U_MANAGER>" in web_client.chat_update.call_args.kwargs["text"]


def test_non_approver_gets_ephemeral_rejection(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    resolver = ScriptedResolver(
        ResolveOutcome(status_code=403, detail="you are not an approver")
    )
    app, web_client = _build(config, redis_client, resolver)
    handler = SocketModeHandler(app, app_token="xapp-test")

    handler.handle(
        FakeSocketClient(),
        _approval_click("env-f1", action_id=APPROVE_ACTION_ID, user="U_OUTSIDER"),
    )
    _drain(app)

    web_client.chat_postEphemeral.assert_called_once()
    kwargs = web_client.chat_postEphemeral.call_args.kwargs
    assert kwargs["user"] == "U_OUTSIDER"
    assert "not an approver" in kwargs["text"]
    # The card is untouched and no turn was enqueued.
    web_client.chat_update.assert_not_called()
    web_client.chat_postMessage.assert_not_called()


def test_claim_race_loser_sees_already_resolved_by_winner(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    resolver = ScriptedResolver(
        ResolveOutcome(
            status_code=409,
            detail="already resolved by U_FIRST (approved)",
            resolved_by=None,
        )
    )
    app, web_client = _build(config, redis_client, resolver)
    handler = SocketModeHandler(app, app_token="xapp-test")

    handler.handle(
        FakeSocketClient(),
        _approval_click("env-l1", action_id=APPROVE_ACTION_ID, user="U_SECOND"),
    )
    _drain(app)

    kwargs = web_client.chat_postEphemeral.call_args.kwargs
    assert kwargs["user"] == "U_SECOND"
    assert "already resolved by U_FIRST" in kwargs["text"]
    # The stale card is refreshed so it stops offering buttons.
    assert web_client.chat_update.call_count == 1
    assert all(
        b["type"] != "actions"
        for b in web_client.chat_update.call_args.kwargs["blocks"]
    )


def test_expired_click_gets_ephemeral_expiry_notice(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    resolver = ScriptedResolver(ResolveOutcome(status_code=410, detail="expired"))
    app, web_client = _build(config, redis_client, resolver)
    handler = SocketModeHandler(app, app_token="xapp-test")

    handler.handle(FakeSocketClient(), _approval_click("env-x1", action_id=APPROVE_ACTION_ID))
    _drain(app)

    assert "expired" in web_client.chat_postEphemeral.call_args.kwargs["text"]
