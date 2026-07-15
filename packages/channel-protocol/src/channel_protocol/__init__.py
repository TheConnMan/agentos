"""Rendering-neutral messages shared by AgentOS channel adapters."""

from .models import (
    MESSAGE_VERSION,
    Action,
    ChannelCapabilities,
    ChannelCapability,
    ChoiceIntent,
    ConfirmIntent,
    InteractionIntent,
    MessageField,
    MessageLink,
    OutboundMessage,
)

__all__ = [
    "MESSAGE_VERSION",
    "Action",
    "ChannelCapability",
    "ChannelCapabilities",
    "ChoiceIntent",
    "ConfirmIntent",
    "InteractionIntent",
    "MessageField",
    "MessageLink",
    "OutboundMessage",
]
