"""The Slack output seam: edit the dispatcher's placeholder in place.

The dispatcher already posted a placeholder message and put its ``ts`` on the
queue. As the runner streams, the kernel edits that same message via
``chat.update`` (a throttled live edit), then writes the final text. This is the
one external service the kernel tests mock; everything else runs for real.
"""

from __future__ import annotations

import logging
from typing import Protocol

from slack_sdk.web.async_client import AsyncWebClient

from .mrkdwn import to_mrkdwn

logger = logging.getLogger(__name__)


class SlackSink(Protocol):
    """Edit a message in place. The kernel throttles how often it calls this."""

    async def update(self, *, channel: str, ts: str, text: str) -> None: ...

    async def clear_status(self, *, channel: str, thread_ts: str) -> None:
        """Clear any assistant-thread status (the "shimmer") on the thread. A
        no-op when shimmering is off; best-effort so it never breaks a turn."""
        ...


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

    async def clear_status(self, *, channel: str, thread_ts: str) -> None:
        # Clear the assistant-thread status by setting it empty (Slack only
        # auto-clears on a posted message, and we edit the placeholder instead).
        # Best-effort: a workspace without the assistant feature, or any transient
        # error, must never fail the turn -- the reply already went out.
        try:
            await self._client.assistant_threads_setStatus(
                channel_id=channel, thread_ts=thread_ts, status=""
            )
        except Exception as exc:  # noqa: BLE001 -- status clear is best-effort
            logger.debug("assistant clear_status skipped for %s: %s", thread_ts, exc)
