"""The approval authorizer: who may resolve a pending approval (#246, ADR-0010).

This is the seam docs/interfaces/approval/INTERFACE.md calls the black line: a
server-side decision, at resolution time, of whether a given actor is allowed
to resolve a given pending approval. It runs HERE, on the server that owns the
durable ``Approval`` record, so it cannot be spoofed from inside the agent's
sandbox -- the runner and the bundle never participate in the decision.

The ``Authorizer`` protocol is the swappable interface; implementations planned
by the epic are channel membership (this module), user-group, explicit
user-list, and platform RBAC. The first implementation's membership evidence is
the card click itself: the worker routes the approval card into the approval's
channel, Slack only renders (and accepts clicks on) that message for members of
that channel, and the click reaches the platform over the dispatcher's
authenticated Socket Mode connection. The dispatcher relays the click's channel
as ``actor_channel``; matching it against the record's channel is therefore a
channel-membership proof for Slack-originated resolutions. Callers that are not
the dispatcher (an operator's curl, the CLI) authenticate with the platform API
key and assert the channel explicitly.

Self-approval is blocked unconditionally: the actor who authored the turn that
raised the request may not resolve it, whatever channel they click from.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import Approval


@dataclass(frozen=True)
class AuthzDecision:
    """The verdict plus the human-readable reason rendered to the clicker."""

    allowed: bool
    reason: str = ""


class Authorizer(Protocol):
    """Decide whether ``actor`` may resolve ``approval`` (server-side, at
    resolution time). ``actor_channel`` is the channel the resolution attempt
    was made from (the card click's channel), or None when the caller supplied
    no channel evidence."""

    def authorize(
        self, approval: Approval, actor: str, actor_channel: str | None
    ) -> AuthzDecision: ...


class ChannelMembershipAuthorizer:
    """First authorizer: the approval's channel members may resolve it.

    Membership evidence is the click channel (see the module docstring): an
    attempt is allowed only when it comes from the channel the approval card
    was routed to -- ``card_channel`` when a route binding placed the card
    (#247), else the requesting channel -- and never from the requester
    themselves.
    """

    def authorize(
        self, approval: Approval, actor: str, actor_channel: str | None
    ) -> AuthzDecision:
        if actor == approval.author:
            return AuthzDecision(
                allowed=False,
                reason="self-approval is blocked: the requester cannot resolve their own request",
            )
        approvers_channel = approval.card_channel or approval.reply_channel
        if actor_channel != approvers_channel:
            return AuthzDecision(
                allowed=False,
                reason="you are not an approver: resolve this from the approval's channel",
            )
        return AuthzDecision(allowed=True)
