"""The Slack output seam: edit the dispatcher's placeholder in place.

The dispatcher already posted a placeholder message and put its ``ts`` on the
queue. As the runner streams, the kernel edits that same message via
``chat.update`` (a throttled live edit), then writes the final text. This is the
one external service the kernel tests mock; everything else runs for real.
"""

from __future__ import annotations

from typing import Protocol

from slack_sdk.web.async_client import AsyncWebClient


class SlackSink(Protocol):
    """Edit a message in place. The kernel throttles how often it calls this."""

    async def update(self, *, channel: str, ts: str, text: str) -> None: ...


class AsyncSlackSink:
    """A SlackSink backed by the Slack Web API (chat.update)."""

    def __init__(self, token: str) -> None:
        self._client = AsyncWebClient(token=token)

    async def update(self, *, channel: str, ts: str, text: str) -> None:
        await self._client.chat_update(channel=channel, ts=ts, text=text)
