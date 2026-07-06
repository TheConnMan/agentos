"""The Slack output seam: edit the dispatcher's placeholder in place.

The dispatcher already posted a placeholder message and put its ``ts`` on the
queue. As the runner streams, the kernel edits that same message via
``chat.update`` (a throttled live edit), then writes the final text. This is the
one external service the kernel tests mock; everything else runs for real.
"""

from __future__ import annotations

from typing import Protocol

from slack_sdk.web.async_client import AsyncWebClient

from .mrkdwn import to_mrkdwn


class SlackSink(Protocol):
    """Edit a message in place. The kernel throttles how often it calls this."""

    async def update(self, *, channel: str, ts: str, text: str) -> None: ...


class AsyncSlackSink:
    """A SlackSink backed by the Slack Web API (chat.update).

    ``base_url`` overrides the Slack API endpoint when set (e.g. a local Slack
    stub for the no-Slack middle-mode e2e); unset leaves the SDK default (real
    Slack), so behavior is unchanged when the override is absent.
    """

    def __init__(self, token: str, *, base_url: str | None = None) -> None:
        if base_url:
            self._client = AsyncWebClient(token=token, base_url=base_url)
        else:
            self._client = AsyncWebClient(token=token)

    async def update(self, *, channel: str, ts: str, text: str) -> None:
        # The runner emits Markdown; Slack renders mrkdwn. Convert here, at the
        # real-Slack seam, so both streamed partials and the final edit render
        # correctly without the kernel knowing about Slack's dialect.
        await self._client.chat_update(channel=channel, ts=ts, text=to_mrkdwn(text))
