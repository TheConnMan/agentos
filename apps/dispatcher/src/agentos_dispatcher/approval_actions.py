"""The click-to-resolve flow for approval cards (#246, ADR-0010).

The worker posts a Block Kit approval card whose Approve/Reject buttons carry
these action ids; a click arrives here over the authenticated Socket Mode
websocket and is forwarded to the platform API's resolve endpoint, where the
authorizer decides server-side whether this actor may resolve (channel
membership, self-approval block). The dispatcher never decides authorization
itself -- it relays who clicked and from which channel, and renders the API's
verdict back into Slack:

- the winner's card is edited in place (buttons removed, verdict stamped);
- a non-approver gets the ephemeral "you are not an approver" rejection;
- a loser of the claim race gets the ephemeral "already resolved by X";
- an expired record gets the ephemeral expiry notice.

The action-id constants live here (not in the worker, which renders the card)
because the worker already depends on this package for the queue seam; the
card renderer imports them so the two sides cannot drift.
"""

import logging
from dataclasses import dataclass
from typing import Any

import httpx
from slack_sdk.web import WebClient

from .config import DispatcherConfig

logger = logging.getLogger(__name__)

# Block Kit action ids for the approval card's buttons. The button ``value``
# carries the approval record id. process_action's catch-all must skip these
# (Bolt runs every matching listener, so without the skip a click would ALSO
# be normalized into an ordinary turn).
APPROVE_ACTION_ID = "agentos-approval-approve"
REJECT_ACTION_ID = "agentos-approval-reject"
_APPROVAL_ACTION_IDS = frozenset({APPROVE_ACTION_ID, REJECT_ACTION_ID})


def is_approval_action(action_id: str) -> bool:
    """True when a Block Kit action id belongs to the approval card."""

    return action_id in _APPROVAL_ACTION_IDS


@dataclass(frozen=True)
class ResolveOutcome:
    """The API's verdict on one resolution attempt, normalized for rendering."""

    status_code: int
    detail: str = ""
    resolved_by: str | None = None
    decision: str | None = None


class ApprovalResolveClient:
    """Thin client for POST /approvals/{id}/resolve (shared API key auth)."""

    def __init__(
        self, *, api_base_url: str, api_key: str, client: httpx.Client | None = None
    ) -> None:
        self._base = api_base_url.rstrip("/")
        self._headers = {"X-API-Key": api_key} if api_key else {}
        self._client = client or httpx.Client(timeout=10.0)

    def resolve(
        self,
        approval_id: str,
        *,
        decision: str,
        resolved_by: str,
        actor_channel: str,
    ) -> ResolveOutcome:
        try:
            response = self._client.post(
                f"{self._base}/approvals/{approval_id}/resolve",
                json={
                    "decision": decision,
                    "resolved_by": resolved_by,
                    "actor_channel": actor_channel,
                },
                headers=self._headers,
            )
        except httpx.HTTPError as exc:
            logger.warning("approval resolve call failed for %s: %s", approval_id, exc)
            return ResolveOutcome(status_code=0, detail=str(exc))
        detail = ""
        resolved = None
        decided = None
        try:
            body = response.json()
            if isinstance(body, dict):
                detail = str(body.get("detail", ""))
                resolved = body.get("resolved_by")
                decided = body.get("status")
        except ValueError:
            # A non-JSON body (e.g. from an ingress/proxy/WAF intermediary)
            # may carry an internal hostname or request id. Do not surface
            # that raw text to the Slack clicker; fall back to empty so the
            # caller's class-neutral fallback applies instead (#453 LOW-1).
            detail = ""
        return ResolveOutcome(
            status_code=response.status_code,
            detail=detail,
            resolved_by=str(resolved) if resolved else None,
            decision=str(decided) if decided else None,
        )


def _resolved_card_blocks(original: dict[str, Any], verdict: str) -> list[dict[str, Any]]:
    """The clicked card with its buttons replaced by the verdict line.

    Every non-actions block of the original message is kept (the summary stays
    readable in place); the actions block is swapped for a context line naming
    the decision and the resolver, so the card cannot be clicked twice.
    """

    blocks = [
        b for b in original.get("blocks", []) if b.get("type") != "actions"
    ]
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": verdict}],
        }
    )
    return blocks


def process_approval_action(
    *,
    body: dict[str, Any],
    decision: str,
    web_client: WebClient,
    resolver: ApprovalResolveClient,
    logger: logging.Logger | None = None,
) -> ResolveOutcome | None:
    """Resolve one card click and render the verdict back into Slack.

    Returns the API outcome (for tests/logging), or None when the interaction
    payload is not a resolvable card click (missing value/channel/message).
    """

    log = logger or logging.getLogger(__name__)

    actions = body.get("actions") or []
    approval_id = str(actions[0].get("value") or "") if actions else ""
    channel = (body.get("channel") or {}).get("id") or ""
    user = (body.get("user") or {}).get("id") or ""
    message = body.get("message") or {}
    card_ts = message.get("ts") or ""
    if not approval_id or not channel or not user or not card_ts:
        log.info("approval action without id/channel/user/message, skipping")
        return None

    outcome = resolver.resolve(
        approval_id, decision=decision, resolved_by=user, actor_channel=channel
    )

    if outcome.status_code == 200:
        verdict = f"{(outcome.decision or decision).capitalize()} by <@{user}>"
        # Best-effort: the record is already resolved and the resume turn is
        # enqueued; a failed card edit must not undo either.
        try:
            web_client.chat_update(
                channel=channel,
                ts=card_ts,
                text=verdict,
                blocks=_resolved_card_blocks(message, verdict),
            )
        except Exception as exc:  # noqa: BLE001 - render is best-effort
            log.warning("approval card update failed for %s: %s", approval_id, exc)
        log.info("approval %s %s by %s", approval_id, decision, user)
        return outcome

    if outcome.status_code == 403:
        # Render the API's reason verbatim: it already knows which class of
        # refusal this is (non-membership, self-approval, or could-not-verify)
        # and words each one distinctly. When the detail is empty or missing,
        # the dispatcher cannot know the class, so the fallback must stay
        # class-neutral rather than guessing at a policy denial (#453 AC5).
        ephemeral = (
            outcome.detail.strip()
            or "This click was refused and the platform gave no reason."
        )
    elif outcome.status_code == 409:
        ephemeral = (
            f"Already resolved by {outcome.resolved_by}."
            if outcome.resolved_by
            else (outcome.detail or "This request was already resolved.")
        )
        # Refresh a stale card so it stops offering buttons for a settled
        # record (the winner's edit normally did this; a race can leave it).
        _refresh_settled_card(
            web_client, channel=channel, card_ts=card_ts, message=message,
            detail=ephemeral, log=log,
        )
    elif outcome.status_code == 410:
        ephemeral = "This approval expired and can no longer be resolved."
    elif outcome.status_code == 404:
        ephemeral = "This approval no longer exists."
    else:
        ephemeral = "Resolving failed; try again shortly."

    try:
        web_client.chat_postEphemeral(channel=channel, user=user, text=ephemeral)
    except Exception as exc:  # noqa: BLE001 - the verdict stands regardless
        log.warning("ephemeral notice failed for %s: %s", approval_id, exc)
    log.info(
        "approval %s click by %s rejected: HTTP %s %s",
        approval_id,
        user,
        outcome.status_code,
        outcome.detail,
    )
    return outcome


def _refresh_settled_card(
    web_client: WebClient,
    *,
    channel: str,
    card_ts: str,
    message: dict[str, Any],
    detail: str,
    log: logging.Logger,
) -> None:
    try:
        web_client.chat_update(
            channel=channel,
            ts=card_ts,
            text=detail,
            blocks=_resolved_card_blocks(message, detail),
        )
    except Exception as exc:  # noqa: BLE001 - best-effort refresh
        log.debug("settled-card refresh skipped: %s", exc)


def build_resolver(config: DispatcherConfig) -> ApprovalResolveClient:
    """The production resolver, from the dispatcher's API settings."""

    return ApprovalResolveClient(
        api_base_url=config.api_base_url, api_key=config.api_key
    )
