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

from .behaviorpacks import NavPack
from .blocks import render

logger = logging.getLogger(__name__)


class SlackSink(Protocol):
    """Edit a message in place. The kernel throttles how often it calls this."""

    async def update(
        self, *, channel: str, ts: str, text: str, nav: NavPack | None = None
    ) -> None: ...

    async def set_status(self, *, channel: str, thread_ts: str, status: str) -> None:
        """Set the assistant-thread status (the "shimmer" caption) on the thread.
        Best-effort so it never breaks a turn."""
        ...

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

    async def update(
        self, *, channel: str, ts: str, text: str, nav: NavPack | None = None
    ) -> None:
        # The runner emits Markdown; Slack renders mrkdwn. ``render`` converts to
        # mrkdwn and, if the reply carries a complete ``agentos-reply`` block,
        # returns Block Kit to render instead -- keeping this dialect/structure
        # knowledge at the real-Slack seam, out of the kernel. A half-streamed or
        # malformed block falls back to text, so partials never show raw JSON.
        # ``nav`` (the agent's hub-button pack, threaded from the kernel) appends
        # the no-dead-ends hub button to a structured reply; None leaves it be.
        rendered_text, blocks = render(text, nav)
        if blocks is not None:
            await self._client.chat_update(
                channel=channel, ts=ts, text=rendered_text, blocks=blocks
            )
        else:
            await self._client.chat_update(channel=channel, ts=ts, text=rendered_text)

    async def set_status(self, *, channel: str, thread_ts: str, status: str) -> None:
        # Best-effort: a workspace without the assistant feature, or any transient
        # error, must never fail the turn.
        try:
            await self._client.assistant_threads_setStatus(
                channel_id=channel, thread_ts=thread_ts, status=status
            )
        except Exception as exc:  # noqa: BLE001 -- status set is best-effort
            logger.debug("assistant set_status skipped for %s: %s", thread_ts, exc)

    async def clear_status(self, *, channel: str, thread_ts: str) -> None:
        # Clear the assistant-thread status by setting it empty (Slack only
        # auto-clears on a posted message, and we edit the placeholder instead).
        await self.set_status(channel=channel, thread_ts=thread_ts, status="")
