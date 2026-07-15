import pytest
from channel_protocol import Action, ChoiceIntent, ConfirmIntent, OutboundMessage
from pydantic import ValidationError


def test_choice_message_round_trips() -> None:
    message = OutboundMessage(
        version="1.0",
        text="Pick a repository.",
        interaction=ChoiceIntent(
            kind="choice",
            id="repo",
            options=[Action(label="AgentOS", value="curie-eng/agentos")],
        ),
    )
    decoded = OutboundMessage.model_validate_json(message.model_dump_json())
    assert isinstance(decoded.interaction, ChoiceIntent)
    assert decoded.interaction.options[0].value == "curie-eng/agentos"


def test_confirm_is_semantic_and_free_text_is_off_by_default() -> None:
    message = OutboundMessage(
        version="1.0",
        text="Deploy this change?",
        interaction=ConfirmIntent(
            kind="confirm",
            id="deploy",
            prompt="Deploy this change?",
            confirm=Action(label="Deploy", value="deploy"),
            cancel=Action(label="Cancel", value="cancel"),
        ),
    )
    assert isinstance(message.interaction, ConfirmIntent)
    assert message.interaction.allow_free_text is False


def test_text_fallback_is_required_and_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        OutboundMessage.model_validate({"version": "1.0"})
    with pytest.raises(ValidationError):
        OutboundMessage.model_validate({"version": "1.0", "text": "ok", "blocks": []})
