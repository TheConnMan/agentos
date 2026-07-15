"""ChannelMembershipAuthorizer unit tests (#246): pure decisions, no I/O."""

from agentos_api.authorizer import ChannelMembershipAuthorizer
from agentos_api.models import Approval


def _approval(*, author: str = "U_AE", channel: str = "C_MGRS") -> Approval:
    return Approval(
        conversation_id="th-1",
        author=author,
        summary="Discount for ACME",
        reply_channel=channel,
        reply_placeholder="p-1",
        dedupe_key="ev-1",
    )


def test_member_of_the_approval_channel_is_allowed() -> None:
    decision = ChannelMembershipAuthorizer().authorize(_approval(), "U_MANAGER", "C_MGRS")
    assert decision.allowed


def test_wrong_or_missing_channel_is_denied() -> None:
    authorizer = ChannelMembershipAuthorizer()
    for channel in ("C_OTHER", None, ""):
        decision = authorizer.authorize(_approval(), "U_MANAGER", channel)
        assert not decision.allowed
        assert "not an approver" in decision.reason


def test_self_approval_is_denied_even_from_the_right_channel() -> None:
    decision = ChannelMembershipAuthorizer().authorize(_approval(author="U_AE"), "U_AE", "C_MGRS")
    assert not decision.allowed
    assert "self-approval" in decision.reason
