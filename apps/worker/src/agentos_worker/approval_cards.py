"""Remember where an approval's Slack card was posted, so an expiry can disable it.

When the kernel pauses a run for approval (#246, ADR-0010) it posts a Block Kit
card with live Approve/Reject buttons. On resolution the dispatcher edits that
card in place from the click's interaction payload. An EXPIRY has no click
(#419): the #412 sweeper -- or a resolve attempt that arrives past the SLA --
flips the record to ``expired`` and enqueues a platform-authored resume turn,
but nothing ever touched the card, so its buttons keep looking live.

This tiny Valkey store bridges what the click payload would otherwise carry: the
kernel remembers the card's channel/ts/summary keyed by the suspended thread at
pause time, and pops it when the expiry resume turn arrives to disable the card.
Keyed by thread because a suspended thread has exactly one pending approval at a
time; a later approval on the same thread overwrites the entry, and the resume
turn (resolve OR expiry) pops it either way.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from redis.asyncio import Redis

from .config import WorkerConfig

# The card outlives the turn that posted it by however long the approval SLA runs
# (hours to days), so its memory must too. A fixed, generous ceiling avoids a new
# boot-env knob: an entry that outlives this is only ever cleaned up by TTL, and a
# card whose memory lapsed simply is not auto-disabled (the resolve-click path
# still heals it on the next interaction).
DEFAULT_CARD_TTL_S = 14 * 24 * 60 * 60


@dataclass(frozen=True)
class ApprovalCardRef:
    """Where a posted approval card lives, enough to edit it in place later."""

    channel: str
    ts: str
    summary: str
    endpoint: str | None = None


class ApprovalCardStore:
    """Valkey memory of posted approval cards, keyed by suspended thread."""

    def __init__(
        self, redis: Redis, config: WorkerConfig, *, ttl_s: int = DEFAULT_CARD_TTL_S
    ) -> None:
        self._redis = redis
        self._config = config
        self._ttl_s = ttl_s

    async def remember(
        self,
        thread: str,
        *,
        channel: str,
        ts: str,
        summary: str,
        endpoint: str | None,
    ) -> None:
        payload = json.dumps(
            {"channel": channel, "ts": ts, "summary": summary, "endpoint": endpoint}
        )
        await self._redis.set(
            self._config.approval_card_key(thread), payload, ex=self._ttl_s
        )

    async def pop(self, thread: str) -> ApprovalCardRef | None:
        """Return and delete the remembered card for this thread, or None.

        GETDEL so the memory is consumed exactly once: a redelivered resume turn
        finds nothing and no-ops, and a resolved card (the dispatcher's job) is
        cleaned up here rather than lingering to the TTL.
        """

        raw = await self._redis.getdel(self._config.approval_card_key(thread))
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return ApprovalCardRef(
                channel=str(data["channel"]),
                ts=str(data["ts"]),
                summary=str(data["summary"]),
                endpoint=data.get("endpoint"),
            )
        except (ValueError, KeyError, TypeError):
            # A corrupt or shape-drifted entry must not break the resume; treat
            # it as no remembered card (the click path can still heal the card).
            return None
