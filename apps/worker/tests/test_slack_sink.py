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
