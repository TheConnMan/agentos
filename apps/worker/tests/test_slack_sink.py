"""AsyncSlackSink base_url override (the no-Slack middle-mode e2e seam)."""

from __future__ import annotations

import asyncio

import pytest
from agentos_worker.config import WorkerConfig
from agentos_worker.slack_sink import AsyncSlackSink


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
