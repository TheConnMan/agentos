"""The group-membership port the approval authorizer decides on (#420, ADR-0034).

The user-group authorizer needs to know who is in a group, and the platform must
not take that answer from its caller: any platform-key holder could then assert
their own membership, which is not authorization but a formality. So the API
resolves membership ITSELF, through this port, and the authorizer decides on what
an implementation returns.

The port is what keeps that resolution a contained dependency. #420 is the first
outbound call ``apps/api`` makes to a chat provider, and the authorizer -- the
platform's decision -- must not be the thing holding the provider's client. There
is one adapter today, and ``main`` is the only module that names it, because
selecting an adapter is wiring.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class UserGroupLookupError(Exception):
    """A usergroup lookup that did not produce a member set.

    The authorizer maps this to a fail-closed denial, so it is raised for every
    failure mode rather than returning an empty set: "nobody is in this group"
    and "we could not find out" are different facts and must stay different.
    """


@dataclass(frozen=True)
class UserGroupMembership:
    """A group's member set plus the provenance the audit row records: when the
    membership was fetched, and how stale the answer that decided is."""

    group: str
    users: frozenset[str]
    fetched_at: datetime
    cache_age_s: float


class GroupMembershipSource(Protocol):
    """Resolve a group's members, server-side, at resolution time.

    An implementation raises ``UserGroupLookupError`` for EVERY mode that yields
    no member set, and never stands an empty ``UserGroupMembership`` in for one:
    the authorizer fails closed on the error and denies as a non-member on the
    empty set, so conflating them would tell a clicker that policy refused them
    when an outage did.
    """

    async def members(self, group_id: str) -> UserGroupMembership: ...
