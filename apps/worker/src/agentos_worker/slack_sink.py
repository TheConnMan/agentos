"""The Slack output seam: edit the dispatcher's placeholder in place.

The dispatcher already posted a placeholder message and put its ``ts`` on the
queue. As the runner streams, the kernel edits that same message via
``chat.update`` (a throttled live edit), then writes the final text. This is the
one external service the kernel tests mock; everything else runs for real.
"""

from __future__ import annotations

import logging
from typing import Protocol

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from .behaviorpacks import NavPack
from .blocks import render

logger = logging.getLogger(__name__)


class SlackSink(Protocol):
    """Edit a message in place. The kernel throttles how often it calls this.

    ``endpoint`` is the per-turn reply target (a Slack Web API base URL) carried
    on the queue payload's reply handle (issue #19). ``None`` routes to the sink's
    configured default, so a real Slack workspace and a no-Slack CLI stub can
    each finalize their own turns on one worker.
    """

    async def update(
        self,
        *,
        channel: str,
        ts: str,
        text: str,
        nav: NavPack | None = None,
        endpoint: str | None = None,
    ) -> None: ...

    async def set_status(
        self, *, channel: str, thread_ts: str, status: str, endpoint: str | None = None
    ) -> None:
        """Set the assistant-thread status (the "shimmer" caption) on the thread.
        Best-effort so it never breaks a turn."""
        ...

    async def clear_status(
        self, *, channel: str, thread_ts: str, endpoint: str | None = None
    ) -> None:
        """Clear any assistant-thread status (the "shimmer") on the thread. A
        no-op when shimmering is off; best-effort so it never breaks a turn."""
        ...


class AsyncSlackSink:
    """A SlackSink backed by the Slack Web API (chat.update).

    The constructor ``base_url`` is the worker's DEFAULT reply endpoint: unset
    leaves the SDK default (real Slack); set points the default at a stub. Each
    call may override it with a per-turn ``endpoint`` (issue #19), so replies
    route back to the ingress that enqueued the turn instead of a single
    worker-global endpoint. One ``AsyncWebClient`` is built and cached per distinct
    base URL, since the SDK binds the endpoint at client construction.
    """

    def __init__(self, token: str, *, base_url: str | None = None) -> None:
        self._token = token
        # Normalize "" to None so the default and an explicit-empty override map to
        # the same (real-Slack) client.
        self._default_base_url = base_url or None
        self._clients: dict[str | None, AsyncWebClient] = {}

    def _client_for(self, endpoint: str | None) -> AsyncWebClient:
        """The cached client for this turn's endpoint, or the worker default.

        A per-turn ``endpoint`` overrides the default; ``None`` (or empty) uses the
        default. Clients are cached per base URL because the SDK binds the endpoint
        at construction, so this never rebuilds a client for a repeat endpoint.
        """
        base_url = endpoint or self._default_base_url
        client = self._clients.get(base_url)
        if client is None:
            client = (
                AsyncWebClient(token=self._token, base_url=base_url)
                if base_url
                else AsyncWebClient(token=self._token)
            )
            self._clients[base_url] = client
        return client

    async def update(
        self,
        *,
        channel: str,
        ts: str,
        text: str,
        nav: NavPack | None = None,
        endpoint: str | None = None,
    ) -> None:
        # The runner emits Markdown; Slack renders mrkdwn. ``render`` converts to
        # mrkdwn and, if the reply carries a complete ``agentos-reply`` block,
        # returns Block Kit to render instead -- keeping this dialect/structure
        # knowledge at the real-Slack seam, out of the kernel. A half-streamed or
        # malformed block falls back to text, so partials never show raw JSON.
        # ``nav`` (the agent's hub-button pack, threaded from the kernel) appends
        # the no-dead-ends hub button to a structured reply; None leaves it be.
        client = self._client_for(endpoint)
        rendered_text, blocks = render(text, nav)
        if blocks is not None:
            # Slack rejects the whole message if a block exceeds a limit we did not
            # clamp; fall back to a text-only update so the turn still completes and
            # the stream entry is acked instead of re-enqueuing the paid model turn.
            try:
                await client.chat_update(
                    channel=channel, ts=ts, text=rendered_text, blocks=blocks
                )
            except SlackApiError:
                logger.warning(
                    "chat_update with blocks rejected for %s; retrying text-only", ts
                )
                await client.chat_update(channel=channel, ts=ts, text=rendered_text)
        else:
            await client.chat_update(channel=channel, ts=ts, text=rendered_text)

    async def set_status(
        self, *, channel: str, thread_ts: str, status: str, endpoint: str | None = None
    ) -> None:
        # Best-effort: a workspace without the assistant feature, or any transient
        # error, must never fail the turn.
        try:
            await self._client_for(endpoint).assistant_threads_setStatus(
                channel_id=channel, thread_ts=thread_ts, status=status
            )
        except Exception as exc:  # noqa: BLE001 -- status set is best-effort
            logger.debug("assistant set_status skipped for %s: %s", thread_ts, exc)

    async def clear_status(
        self, *, channel: str, thread_ts: str, endpoint: str | None = None
    ) -> None:
        # Clear the assistant-thread status by setting it empty (Slack only
        # auto-clears on a posted message, and we edit the placeholder instead).
        await self.set_status(
            channel=channel, thread_ts=thread_ts, status="", endpoint=endpoint
        )
