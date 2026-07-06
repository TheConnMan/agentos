"""Unit tests for the behavior-pack sampler and greeting matcher (pure, no stack).

These pin the two properties that make packs safe as a per-agent, platform-owned
feature: the greeting short-circuit fires only on a *bare* greeting (never on a
greeting glued to a real request), and tip sampling is deterministic per seed.
"""

from __future__ import annotations

from agentos_worker.behaviorpacks import (
    BehaviorPacks,
    GreetingPack,
    HelpPack,
    TipsPack,
    match_greeting,
    match_help,
    sample_tip,
)

_GREETING = GreetingPack(
    enabled=True,
    phrases=("hi", "hey", "hello", "good morning", "what can you do"),
    reply="Hi! Ask me about revenue leaks.",
)


def _packs(
    *,
    greeting: GreetingPack | None = None,
    tips: TipsPack | None = None,
    help: HelpPack | None = None,
) -> BehaviorPacks:
    return BehaviorPacks(
        greeting=greeting or GreetingPack(),
        tips=tips or TipsPack(),
        help=help or HelpPack(),
    )


# -- greeting matcher ---------------------------------------------------------


def test_bare_greeting_matches() -> None:
    packs = _packs(greeting=_GREETING)
    assert match_greeting(packs, "hi") == _GREETING.reply
    assert match_greeting(packs, "Hey!") == _GREETING.reply
    assert match_greeting(packs, "good morning") == _GREETING.reply


def test_greeting_then_only_filler_matches() -> None:
    packs = _packs(greeting=_GREETING)
    assert match_greeting(packs, "hey there team") == _GREETING.reply
    assert match_greeting(packs, "hello everyone") == _GREETING.reply


def test_elongated_greeting_matches() -> None:
    packs = _packs(greeting=_GREETING)
    assert match_greeting(packs, "hiiii") == _GREETING.reply
    assert match_greeting(packs, "heyyy there") == _GREETING.reply


def test_greeting_glued_to_request_falls_through() -> None:
    # The load-bearing negative: this must reach the model, not the canned reply.
    packs = _packs(greeting=_GREETING)
    assert match_greeting(packs, "hi show me the report") is None
    assert match_greeting(packs, "hey what leaked last quarter") is None


def test_intro_question_matches_only_when_a_phrase() -> None:
    packs = _packs(greeting=_GREETING)
    assert match_greeting(packs, "what can you do") == _GREETING.reply
    # Not a configured phrase -> falls through.
    assert match_greeting(packs, "who are you") is None


def test_disabled_or_empty_pack_never_matches() -> None:
    assert match_greeting(_packs(), "hi") is None
    disabled = GreetingPack(enabled=False, phrases=("hi",), reply="x")
    assert match_greeting(_packs(greeting=disabled), "hi") is None
    # Enabled but no reply configured -> nothing to send.
    no_reply = GreetingPack(enabled=True, phrases=("hi",), reply="")
    assert match_greeting(_packs(greeting=no_reply), "hi") is None


def test_empty_message_never_matches() -> None:
    assert match_greeting(_packs(greeting=_GREETING), "   ") is None


# -- help matcher (shares the bare-match core with greeting) ------------------

_HELP = HelpPack(
    enabled=True,
    phrases=("help", "commands", "what can you do"),
    reply="Here is what I can do: ...",
)


def test_bare_help_matches() -> None:
    packs = _packs(help=_HELP)
    assert match_help(packs, "help") == _HELP.reply
    assert match_help(packs, "Commands?") == _HELP.reply
    assert match_help(packs, "what can you do") == _HELP.reply


def test_help_glued_to_request_falls_through() -> None:
    assert match_help(_packs(help=_HELP), "help me reconcile the invoices") is None


def test_help_and_greeting_are_independent() -> None:
    # A help pack does not answer greetings and vice versa.
    packs = _packs(help=_HELP, greeting=_GREETING)
    assert match_help(packs, "hi") is None
    assert match_greeting(packs, "help") is None


def test_help_disabled_or_empty_never_matches() -> None:
    assert match_help(_packs(), "help") is None
    no_reply = HelpPack(enabled=True, phrases=("help",), reply="")
    assert match_help(_packs(help=no_reply), "help") is None


# -- tip sampler --------------------------------------------------------------

_TIPS = TipsPack(
    enabled=True,
    working_lines=("Working on it!", "Crunching the numbers..."),
    tips=("I can rank leaks by recoverable $", "Ask me for the top 5"),
)


def test_sample_tip_is_deterministic_per_seed() -> None:
    packs = _packs(tips=_TIPS)
    assert sample_tip(packs, "ts-123") == sample_tip(packs, "ts-123")


def test_sample_tip_varies_across_seeds() -> None:
    packs = _packs(tips=_TIPS)
    seen = {sample_tip(packs, f"ts-{i}") for i in range(20)}
    assert len(seen) > 1  # not a constant


def test_sample_tip_composes_working_line_and_tip() -> None:
    out = sample_tip(_packs(tips=_TIPS), "seed")
    assert out is not None
    assert "\n\nTip: " in out
    first = out.split("\n\n", 1)[0]
    assert first in _TIPS.working_lines


def test_sample_tip_disabled_or_empty_returns_none() -> None:
    assert sample_tip(_packs(), "seed") is None
    assert sample_tip(_packs(tips=TipsPack(enabled=True)), "seed") is None


def test_sample_tip_working_only_and_tip_only() -> None:
    working_only = TipsPack(enabled=True, working_lines=("Just a sec",))
    assert sample_tip(_packs(tips=working_only), "s") == "Just a sec"
    tip_only = TipsPack(enabled=True, tips=("Try /help",))
    assert sample_tip(_packs(tips=tip_only), "s") == "Tip: Try /help"


def test_from_config_roundtrip_and_none() -> None:
    assert BehaviorPacks.from_config(None) == BehaviorPacks()
    assert BehaviorPacks.from_config({}) == BehaviorPacks()
    parsed = BehaviorPacks.from_config(
        {"greeting": {"enabled": True, "phrases": ["hi"], "reply": "yo"}}
    )
    assert parsed.greeting.enabled is True
    assert match_greeting(parsed, "hi") == "yo"
