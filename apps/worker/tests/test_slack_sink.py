"""AsyncSlackSink base_url override (the no-Slack middle-mode e2e seam)."""

from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest
from agentos_worker.config import WorkerConfig
from agentos_worker.slack_sink import AsyncSlackSink
from channel_protocol import (
    MESSAGE_VERSION,
    Action,
    ConfirmIntent,
    OutboundMessage,
)
from slack_sdk.errors import SlackApiError


def test_base_url_override_passes_through() -> None:
    sink = AsyncSlackSink("xoxb-test", base_url="http://localhost:9999")
    assert sink._client_for(None).base_url == "http://localhost:9999/"


def test_unset_base_url_uses_the_sdk_default() -> None:
    sink = AsyncSlackSink("xoxb-test")
    assert sink._client_for(None).base_url == "https://slack.com/api/"


def test_per_turn_endpoint_routes_to_a_distinct_cached_client() -> None:
    # Issue #19: a turn carrying its own endpoint must post through a client bound
    # to that endpoint, not the worker default; the client is cached per base URL
    # (built once, reused) since the SDK binds the endpoint at construction.
    sink = AsyncSlackSink("xoxb-test", base_url="http://default:1/api/")
    default = sink._client_for(None)
    per_turn = sink._client_for("http://stub:2/api/")

    assert per_turn.base_url == "http://stub:2/api/"
    assert per_turn is not default  # a distinct endpoint gets a distinct client
    assert sink._client_for("http://stub:2/api/") is per_turn  # cached, not rebuilt
    assert sink._client_for(None) is default  # the default is stable too
    # An explicit-empty endpoint collapses onto the worker default, not a third client.
    assert sink._client_for("") is default


def test_update_routes_to_the_per_turn_endpoint() -> None:
    # The endpoint passed to update() selects which client posts the edit.
    sink = AsyncSlackSink("xoxb-test", base_url="http://default:1/api/")
    seen: list[str] = []

    def _record(label: str):
        async def _fake_chat_update(*, channel: str, ts: str, text: str) -> None:
            seen.append(label)

        return _fake_chat_update

    sink._client_for(None).chat_update = _record("default")  # type: ignore[method-assign]
    sink._client_for("http://stub:2/api/").chat_update = _record("stub")  # type: ignore[method-assign]

    asyncio.run(sink.update(channel="C1", ts="1.1", text="a"))  # no endpoint -> default
    asyncio.run(sink.update(channel="C1", ts="1.2", text="b", endpoint="http://stub:2/api/"))

    assert seen == ["default", "stub"]


def test_config_reads_slack_api_base_url_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SLACK_API_BASE_URL", raising=False)
    assert WorkerConfig().slack_api_base_url == ""
    monkeypatch.setenv("SLACK_API_BASE_URL", "http://localhost:9999")
    assert WorkerConfig().slack_api_base_url == "http://localhost:9999"


def test_update_converts_markdown_to_mrkdwn() -> None:
    sink = AsyncSlackSink("xoxb-test")
    captured: dict[str, str] = {}

    async def _fake_chat_update(*, channel: str, ts: str, text: str) -> None:
        captured.update(channel=channel, ts=ts, text=text)

    sink._client_for(None).chat_update = _fake_chat_update  # type: ignore[method-assign]

    asyncio.run(sink.update(channel="C1", ts="1.1", text="**hi** [x](http://y)"))

    assert captured["text"] == "*hi* <http://y|x>"
    assert "blocks" not in captured  # plain text path passes no blocks


def test_update_renders_blocks_for_a_reply_convention() -> None:
    sink = AsyncSlackSink("xoxb-test")
    captured: dict[str, object] = {}

    async def _fake_chat_update(**kwargs: object) -> None:
        captured.update(kwargs)

    sink._client_for(None).chat_update = _fake_chat_update  # type: ignore[method-assign]

    text = '```agentos-reply\n{"header": "Hi", "text": "body"}\n```'
    asyncio.run(sink.update(channel="C1", ts="1.1", text=text))

    assert isinstance(captured.get("blocks"), list)
    assert captured["blocks"][0]["type"] == "header"  # type: ignore[index]
    assert captured["text"] == "body"  # accessibility fallback, not raw JSON


def test_clear_status_sets_empty_assistant_status() -> None:
    sink = AsyncSlackSink("xoxb-test")
    captured: dict[str, str] = {}

    async def _fake_set_status(*, channel_id: str, thread_ts: str, status: str) -> None:
        captured.update(channel_id=channel_id, thread_ts=thread_ts, status=status)

    sink._client_for(None).assistant_threads_setStatus = _fake_set_status  # type: ignore[method-assign]

    asyncio.run(sink.clear_status(channel="C1", thread_ts="1.1"))

    assert captured == {"channel_id": "C1", "thread_ts": "1.1", "status": ""}


def test_clear_status_is_best_effort_on_error() -> None:
    sink = AsyncSlackSink("xoxb-test")

    async def _boom(**_kwargs: object) -> None:
        raise RuntimeError("workspace has no assistant feature")

    sink._client_for(None).assistant_threads_setStatus = _boom  # type: ignore[method-assign]

    # Must not raise -- clearing the shimmer can never fail a completed turn.
    asyncio.run(sink.clear_status(channel="C1", thread_ts="1.1"))


# --- #31: no-edit streaming config + status/links pass-through ----------------


def test_config_no_edit_streaming_defaults_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTOS_SLACK_NO_EDIT_STREAMING", raising=False)
    assert WorkerConfig().slack_no_edit_streaming is False


def test_config_reads_no_edit_streaming_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTOS_SLACK_NO_EDIT_STREAMING", "true")
    assert WorkerConfig().slack_no_edit_streaming is True


def test_update_renders_status_and_link_blocks_for_a_reply() -> None:
    sink = AsyncSlackSink("xoxb-test")
    captured: dict[str, object] = {}

    async def _fake_chat_update(**kwargs: object) -> None:
        captured.update(kwargs)

    sink._client_for(None).chat_update = _fake_chat_update  # type: ignore[method-assign]

    text = (
        '```agentos-reply\n'
        '{"status": "Working", "text": "body", "links": [["Docs", "https://x/y"]]}\n'
        '```'
    )
    asyncio.run(sink.update(channel="C1", ts="1.1", text=text))

    blocks = captured.get("blocks")
    assert isinstance(blocks, list)
    assert blocks[0]["type"] == "context"  # status context leads  # type: ignore[index]
    link_actions = [
        b
        for b in blocks
        if b["type"] == "actions" and any("url" in e for e in b["elements"])
    ]
    assert link_actions, "expected an actions block of URL link buttons"
    assert link_actions[0]["elements"][0]["url"] == "https://x/y"


# --- #228: text-only fallback when a blocks update is rejected ----------------
# If Slack rejects the with-blocks chat_update (SlackApiError, e.g. invalid_blocks),
# the reply must still be delivered: retry text-only so the turn completes exactly
# once and never re-enqueues.


def test_update_falls_back_to_text_only_on_slack_api_error() -> None:
    sink = AsyncSlackSink("xoxb-test")
    calls: list[dict[str, object]] = []

    async def _fake_chat_update(**kwargs: object) -> None:
        calls.append(kwargs)
        if "blocks" in kwargs:  # the first (with-blocks) call is rejected
            raise SlackApiError(
                "invalid_blocks", {"ok": False, "error": "invalid_blocks"}
            )

    sink._client_for(None).chat_update = _fake_chat_update  # type: ignore[method-assign]

    text = '```agentos-reply\n{"header": "Hi", "text": "body"}\n```'
    # Must not raise: the rejected blocks update falls back to a text-only update.
    asyncio.run(sink.update(channel="C1", ts="1.1", text=text))

    assert len(calls) == 2
    second = calls[1]
    assert "blocks" not in second  # the retry is text-only
    assert second["text"] == "body"  # same accessibility fallback text


def test_update_fallback_text_stays_within_slack_text_cap() -> None:
    # Loop-closure: model Slack's real failure mode where chat.update rejects
    # ANY call carrying blocks OR text longer than 40000 chars. A reply with a
    # ~60000-char body must still complete: the with-blocks call is rejected,
    # and the text-only retry must send bounded (<=40000) text so it succeeds
    # instead of re-raising and re-opening the unbounded paid-retry loop.
    sink = AsyncSlackSink("xoxb-test")
    calls: list[dict[str, object]] = []

    async def _fake_chat_update(**kwargs: object) -> None:
        text = kwargs.get("text")
        if "blocks" in kwargs or (isinstance(text, str) and len(text) > 40000):
            raise SlackApiError("too_long", {"ok": False, "error": "msg_too_long"})
        calls.append(kwargs)  # only a within-cap text-only call is recorded

    sink._client_for(None).chat_update = _fake_chat_update  # type: ignore[method-assign]

    text = "```agentos-reply\n" + json.dumps({"header": "H", "text": "x" * 60000}) + "\n```"
    # Must not raise: the fallback text is bounded so the retry succeeds.
    asyncio.run(sink.update(channel="C1", ts="1.1", text=text))

    assert calls, "expected a successful text-only update"
    final = calls[-1]
    assert "blocks" not in final
    assert isinstance(final["text"], str)
    assert len(final["text"]) <= 40000


def test_update_does_not_retry_when_blocks_update_succeeds() -> None:
    sink = AsyncSlackSink("xoxb-test")
    calls: list[dict[str, object]] = []

    async def _fake_chat_update(**kwargs: object) -> None:
        calls.append(kwargs)

    sink._client_for(None).chat_update = _fake_chat_update  # type: ignore[method-assign]

    text = '```agentos-reply\n{"header": "Hi", "text": "body"}\n```'
    asyncio.run(sink.update(channel="C1", ts="1.1", text=text))

    assert len(calls) == 1  # a spurious retry would make this 2
    assert isinstance(calls[0].get("blocks"), list)


# --- #530: fall back to the default transport when a resumed reply endpoint is
# unreachable (the ephemeral CLI stub died; its URL is persisted on the Approval).


def _raise_connection_error():
    async def _fake(**_kwargs):
        raise aiohttp.ClientError("connection refused")

    return _fake


def _record_call(sink_calls, label):
    async def _fake(**kwargs):
        sink_calls.append(label)
        return {"ok": True, "ts": "9.9"}

    return _fake


def test_update_falls_back_to_default_when_endpoint_unreachable() -> None:
    sink = AsyncSlackSink("xoxb-test", base_url="http://default:1/api/")
    landed: list[str] = []
    # The per-turn (stub) client's host is dead -> connection error.
    sink._client_for("http://stub:2/api/").chat_update = _raise_connection_error()  # type: ignore[method-assign]
    sink._client_for(None).chat_update = _record_call(landed, "default")  # type: ignore[method-assign]

    asyncio.run(sink.update(channel="C1", ts="1.1", text="hi", endpoint="http://stub:2/api/"))

    assert landed == ["default"], "the resumed reply must land on the default transport"


def test_slack_api_error_is_not_treated_as_unreachable() -> None:
    # A SlackApiError means the endpoint IS reachable but rejected the call; it must
    # NOT trigger a transport fallback (that would misroute a live-workspace reply).
    sink = AsyncSlackSink("xoxb-test", base_url="http://default:1/api/")
    default_hits: list[str] = []

    async def _reject(**_kwargs):
        raise SlackApiError("nope", response={"error": "channel_not_found"})

    sink._client_for("http://stub:2/api/").chat_update = _reject  # type: ignore[method-assign]
    sink._client_for(None).chat_update = _record_call(default_hits, "default")  # type: ignore[method-assign]

    with pytest.raises(SlackApiError):
        asyncio.run(
            sink.update(channel="C1", ts="1.1", text="hi", endpoint="http://stub:2/api/")
        )
    assert default_hits == [], "a SlackApiError must not fall back to the default"


def test_no_fallback_when_endpoint_equals_default() -> None:
    # A turn already on the default transport has no alternate to try; the
    # connection error propagates (there is nowhere else to send it).
    sink = AsyncSlackSink("xoxb-test", base_url="http://default:1/api/")
    sink._client_for(None).chat_update = _raise_connection_error()  # type: ignore[method-assign]

    with pytest.raises(aiohttp.ClientError):
        asyncio.run(sink.update(channel="C1", ts="1.1", text="hi"))  # no endpoint -> default


def test_no_fallback_when_no_default_is_configured() -> None:
    # No worker default (real Slack, no base_url): a dead per-turn endpoint has no
    # safe fallback, so the error propagates rather than guessing a transport.
    sink = AsyncSlackSink("xoxb-test")  # default is the real Slack sentinel
    # Make BOTH the per-turn and the (real-Slack) default raise so a fallback, if it
    # wrongly fired, would still error -- but we assert it never reaches the default.
    default_hits: list[str] = []
    sink._client_for("http://stub:2/api/").chat_update = _raise_connection_error()  # type: ignore[method-assign]
    sink._client_for(None).chat_update = _record_call(default_hits, "default")  # type: ignore[method-assign]

    with pytest.raises(aiohttp.ClientError):
        asyncio.run(
            sink.update(channel="C1", ts="1.1", text="hi", endpoint="http://stub:2/api/")
        )
    assert default_hits == [], "real-Slack default is not a safe fallback for a CLI-stub endpoint"


# --- #708: best-effort resume reply when the endpoint is unreachable and there is
# no distinct default (the pure-offline local loop). The swallow POLICY lives in the
# sink; the "is this a resume turn" DECISION lives in the kernel (_is_approval_resume)
# and reaches the sink as ``best_effort_unreachable``.


def test_best_effort_unreachable_swallows_when_no_default() -> None:
    # A resume turn's reply is best-effort: with no distinct default (offline loop)
    # and a dead per-turn endpoint, best_effort_unreachable makes update
    # log-and-return instead of re-raising, so the resolved approval's turn ACKs
    # instead of dead-lettering. It must also never misroute to the real-Slack
    # default (there is no safe fallback for a CLI-stub endpoint).
    sink = AsyncSlackSink("xoxb-test")  # no base_url -> no distinct default
    default_hits: list[str] = []
    sink._client_for("http://stub:2/api/").chat_update = _raise_connection_error()  # type: ignore[method-assign]
    sink._client_for(None).chat_update = _record_call(default_hits, "default")  # type: ignore[method-assign]

    # Must NOT raise.
    asyncio.run(
        sink.update(
            channel="C1",
            ts="1.1",
            text="hi",
            endpoint="http://stub:2/api/",
            best_effort_unreachable=True,
        )
    )
    assert default_hits == [], "best-effort swallow must not misroute to the default"


def test_best_effort_false_still_raises_when_no_default() -> None:
    # The discriminator guard: without the flag (the default False, i.e. every
    # non-resume turn and the loud _drop_with_message/_escalate paths), a dead
    # endpoint with no default still raises loudly -- unchanged from
    # test_no_fallback_when_no_default_is_configured.
    sink = AsyncSlackSink("xoxb-test")
    sink._client_for("http://stub:2/api/").chat_update = _raise_connection_error()  # type: ignore[method-assign]

    with pytest.raises(aiohttp.ClientError):
        asyncio.run(
            sink.update(
                channel="C1",
                ts="1.1",
                text="hi",
                endpoint="http://stub:2/api/",
                best_effort_unreachable=False,
            )
        )


def test_slack_api_error_not_swallowed_even_with_best_effort() -> None:
    # A SlackApiError means the endpoint IS reachable but rejected the call; it is
    # NOT an unreachability, so best_effort_unreachable must NOT swallow it (that
    # would hide a real delivery rejection). Only transport-unreachable errors
    # (_UNREACHABLE_ERRORS) are in scope for the best-effort resume swallow.
    sink = AsyncSlackSink("xoxb-test")

    async def _reject(**_kwargs):
        raise SlackApiError("nope", response={"error": "channel_not_found"})

    sink._client_for("http://stub:2/api/").chat_update = _reject  # type: ignore[method-assign]

    with pytest.raises(SlackApiError):
        asyncio.run(
            sink.update(
                channel="C1",
                ts="1.1",
                text="hi",
                endpoint="http://stub:2/api/",
                best_effort_unreachable=True,
            )
        )


def test_best_effort_stays_loud_when_default_transport_is_the_dead_target() -> None:
    # F1 (side-effects HIGH): the best-effort swallow must fire ONLY in the pure
    # no-default-configured case. Here a default IS configured and the reply is
    # going over that CONFIGURED default (endpoint=None), so an unreachable error
    # is a genuine transient OUTAGE of a real Slack transport -- it must stay LOUD
    # (raise -> reclaim -> retry per ADR-0039), NOT be swallowed and acked. Even
    # though has_distinct_default is False here, best_effort must not swallow.
    sink = AsyncSlackSink("xoxb-test", base_url="http://default:1/api/")
    sink._client_for(None).chat_update = _raise_connection_error()  # type: ignore[method-assign]

    with pytest.raises(aiohttp.ClientError):
        asyncio.run(
            sink.update(
                channel="C1",
                ts="1.1",
                text="hi",
                endpoint=None,
                best_effort_unreachable=True,
            )
        )


# --- #454: the approval card renders BELOW the seam (ADR-0020) ----------------
# The kernel emits a channel-neutral Confirm intent; the Slack adapter's post()
# renders it into the same Block Kit approval card that used to be built in the
# kernel. These tests are where the byte-identical card contract lives now (the
# builders themselves are unit-tested in test_blocks.py).


def _approval_message(approval_id: str, summary: str) -> OutboundMessage:
    return OutboundMessage(
        version=MESSAGE_VERSION,
        text=summary,
        interaction=ConfirmIntent(
            kind="confirm",
            id=approval_id,
            prompt=summary,
            confirm=Action(label="Approve", value=approval_id),
            cancel=Action(label="Reject", value=approval_id),
        ),
    )


def test_post_renders_the_approval_card_from_a_confirm_intent() -> None:
    # The adapter turns a Confirm intent into the Block Kit approval card: header,
    # the summary section, a "Requested by" context line, and Approve/Reject
    # buttons carrying the dispatcher's action ids + the record id as value.
    from agentos_dispatcher.approval_actions import APPROVE_ACTION_ID, REJECT_ACTION_ID

    sink = AsyncSlackSink("xoxb-test")
    captured: dict[str, object] = {}

    async def _fake_post(**kwargs: object):
        captured.update(kwargs)
        return {"ok": True, "ts": "9.9"}

    sink._client_for(None).chat_postMessage = _fake_post  # type: ignore[method-assign]

    ts = asyncio.run(
        sink.post(
            channel="C1",
            message=_approval_message("appr-1", "Give ACME a 20% discount"),
            requested_by="U_AE",
            thread_ts="th-card",
        )
    )

    assert ts == "9.9"
    assert captured["channel"] == "C1"
    assert captured["thread_ts"] == "th-card"
    assert "Give ACME a 20% discount" in captured["text"]  # accessibility fallback
    blocks = captured["blocks"]
    assert isinstance(blocks, list)
    assert blocks[0]["type"] == "header"  # type: ignore[index]
    assert "Give ACME a 20% discount" in blocks[1]["text"]["text"]  # type: ignore[index]
    assert "<@U_AE>" in blocks[2]["elements"][0]["text"]  # type: ignore[index]
    actions = blocks[-1]  # type: ignore[index]
    assert actions["type"] == "actions"
    approve, reject = actions["elements"]
    assert approve["action_id"] == APPROVE_ACTION_ID
    assert approve["value"] == "appr-1"
    assert approve["style"] == "primary"
    assert reject["action_id"] == REJECT_ACTION_ID
    assert reject["value"] == "appr-1"
    assert reject["style"] == "danger"


def test_post_falls_back_to_text_only_when_card_blocks_rejected() -> None:
    # Mirrors update(): a rejected Block Kit payload retries text-only so the
    # notice still lands rather than losing the message (the API resolve path
    # stands regardless).
    sink = AsyncSlackSink("xoxb-test")
    calls: list[dict[str, object]] = []

    async def _fake_post(**kwargs: object):
        calls.append(kwargs)
        if "blocks" in kwargs:
            raise SlackApiError("invalid_blocks", {"ok": False, "error": "invalid_blocks"})
        return {"ok": True, "ts": "9.9"}

    sink._client_for(None).chat_postMessage = _fake_post  # type: ignore[method-assign]

    ts = asyncio.run(
        sink.post(
            channel="C1",
            message=_approval_message("appr-1", "Discount ACME"),
            requested_by="U1",
        )
    )

    assert ts == "9.9"
    assert len(calls) == 2
    assert "blocks" not in calls[1]  # the retry is text-only
    assert "Discount ACME" in calls[1]["text"]


def test_update_message_renders_the_expired_card() -> None:
    # The adapter rebuilds the settled expired card from the channel-neutral
    # summary: the summary stays, the Approve/Reject actions block is gone, and an
    # expiry line takes its place so the card can no longer be clicked.
    sink = AsyncSlackSink("xoxb-test")
    captured: dict[str, object] = {}

    async def _fake_update(**kwargs: object) -> None:
        captured.update(kwargs)

    sink._client_for(None).chat_update = _fake_update  # type: ignore[method-assign]

    asyncio.run(
        sink.update_message(
            channel="C1",
            ts="9.9",
            message=OutboundMessage(
                version=MESSAGE_VERSION, text="Give ACME a 20% discount"
            ),
        )
    )

    assert captured["channel"] == "C1"
    assert captured["ts"] == "9.9"
    assert "expired" in str(captured["text"]).lower()
    blocks = captured["blocks"]
    assert isinstance(blocks, list)
    assert "Give ACME a 20% discount" in blocks[1]["text"]["text"]  # type: ignore[index]
    assert all(b.get("type") != "actions" for b in blocks)  # type: ignore[union-attr]
    assert any("expired" in str(b).lower() for b in blocks)


def test_best_effort_still_falls_back_to_default_when_present() -> None:
    # #530 stays byte-for-byte: with a distinct default configured, a dead per-turn
    # endpoint on a resume turn STILL falls back to the default transport. The new
    # flag only governs the no-distinct-default branch; it must not alter the
    # has-default path (the reply still LANDS on the default, it is not swallowed).
    sink = AsyncSlackSink("xoxb-test", base_url="http://default:1/api/")
    landed: list[str] = []
    sink._client_for("http://stub:2/api/").chat_update = _raise_connection_error()  # type: ignore[method-assign]
    sink._client_for(None).chat_update = _record_call(landed, "default")  # type: ignore[method-assign]

    asyncio.run(
        sink.update(
            channel="C1",
            ts="1.1",
            text="hi",
            endpoint="http://stub:2/api/",
            best_effort_unreachable=True,
        )
    )
    assert landed == ["default"], "the flag must not bypass the #530 default-transport fallback"
