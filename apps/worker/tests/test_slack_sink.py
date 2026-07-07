"""AsyncSlackSink base_url override (the no-Slack middle-mode e2e seam)."""

from __future__ import annotations

import asyncio

from agentos_worker.config import WorkerConfig
from agentos_worker.slack_sink import AsyncSlackSink


def test_base_url_override_passes_through() -> None:
    sink = AsyncSlackSink("xoxb-test", base_url="http://localhost:9999")
    assert sink._client.base_url == "http://localhost:9999/"


def test_unset_base_url_uses_the_sdk_default() -> None:
    sink = AsyncSlackSink("xoxb-test")
    assert sink._client.base_url == "https://slack.com/api/"


def test_config_reads_slack_api_base_url_from_env() -> None:
    assert WorkerConfig.from_env({}).slack_api_base_url == ""
    cfg = WorkerConfig.from_env({"SLACK_API_BASE_URL": "http://localhost:9999"})
    assert cfg.slack_api_base_url == "http://localhost:9999"


def test_update_converts_markdown_to_mrkdwn() -> None:
    sink = AsyncSlackSink("xoxb-test")
    captured: dict[str, str] = {}

    async def _fake_chat_update(*, channel: str, ts: str, text: str) -> None:
        captured.update(channel=channel, ts=ts, text=text)

    sink._client.chat_update = _fake_chat_update  # type: ignore[method-assign]

    asyncio.run(sink.update(channel="C1", ts="1.1", text="**hi** [x](http://y)"))

    assert captured["text"] == "*hi* <http://y|x>"
    assert "blocks" not in captured  # plain text path passes no blocks


def test_update_renders_blocks_for_a_reply_convention() -> None:
    sink = AsyncSlackSink("xoxb-test")
    captured: dict[str, object] = {}

    async def _fake_chat_update(**kwargs: object) -> None:
        captured.update(kwargs)

    sink._client.chat_update = _fake_chat_update  # type: ignore[method-assign]

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

    sink._client.assistant_threads_setStatus = _fake_set_status  # type: ignore[method-assign]

    asyncio.run(sink.clear_status(channel="C1", thread_ts="1.1"))

    assert captured == {"channel_id": "C1", "thread_ts": "1.1", "status": ""}


def test_clear_status_is_best_effort_on_error() -> None:
    sink = AsyncSlackSink("xoxb-test")

    async def _boom(**_kwargs: object) -> None:
        raise RuntimeError("workspace has no assistant feature")

    sink._client.assistant_threads_setStatus = _boom  # type: ignore[method-assign]

    # Must not raise -- clearing the shimmer can never fail a completed turn.
    asyncio.run(sink.clear_status(channel="C1", thread_ts="1.1"))
