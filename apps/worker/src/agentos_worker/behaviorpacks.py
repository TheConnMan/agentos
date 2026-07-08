"""Behavior packs: declarative, per-agent UX touches applied around a turn.

Touches an agent owner may want, that other owners may not:

- a sampled "working..." line (optionally with a capability tip) shown while a
  turn runs,
- a canned reply to a bare greeting ("hi", "hey there team"), and
- a canned reply to a bare help / "what can you do" request,

each of the last two answered without a model call.

The design principle: packs are DATA, not code. An agent's packs are stored as
JSON on its ``agents`` row (apps/api) and resolved onto the deployment by the
binding layer; the sampler and matchers here are platform-owned and
byte-for-byte identical for every agent -- only the phrases/lines/reply vary per
agent. Keeping packs declarative is deliberate: pack content never executes, so
enabling a pack can never run an agent's code, and the sandbox-isolation
guarantee holds without any pack ever crossing into the runner. It is also why
some template batteries are NOT expressible as packs (they are code, or need a
Block Kit reply model AgentOS does not have) -- see ``docs/behavior-packs.md``.

Pure stdlib (unicodedata, re, zlib): no Slack, no model, no I/O. This module is
the substrate; the kernel wiring that actually calls ``sample_tip`` /
``match_greeting`` / ``match_help`` around a turn is a separate change to the F1
"sacred" kernel and lands under that module's adversarial-review process (see
``docs/behavior-packs.md``).
"""

from __future__ import annotations

import re
import unicodedata
import zlib
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict

# Words that may trail a greeting without making it a real request: "hey there",
# "morning team". A greeting followed only by filler is still a bare greeting;
# a greeting followed by anything else ("hi show me the report") is not, and
# must fall through to the model. Kept small and platform-owned on purpose.
_FILLER = frozenset({"there", "team", "all", "everyone", "folks", "yall", "guys", "peeps", "bot"})

_NON_WORD = re.compile(r"[^a-z0-9\s]+")
_WHITESPACE = re.compile(r"\s+")
_RUN = re.compile(r"(.)\1{2,}")  # 3+ of the same char in a row


class TipsPack(BaseModel):
    """The "working..." acknowledgment content for one agent."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    # Sampled for the first line ("Working on it!", "Crunching the numbers...").
    working_lines: tuple[str, ...] = ()
    # Sampled for an optional capability tip line ("I can rank leaks by $").
    tips: tuple[str, ...] = ()


class GreetingPack(BaseModel):
    """The deterministic greeting short-circuit content for one agent."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    # Trigger phrases/words, matched after normalization: "hi", "hey",
    # "good morning", "what can you do".
    phrases: tuple[str, ...] = ()
    # The canned reply sent on a match (no model call).
    reply: str = ""


class HelpPack(BaseModel):
    """The deterministic help / "what can you do" short-circuit for one agent.

    Same shape as GreetingPack (this is the niceties battery's help half): a bare
    help request gets a canned reply with no model call."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    # Trigger phrases/words: "help", "commands", "what can you do".
    phrases: tuple[str, ...] = ()
    # The canned reply sent on a match (no model call).
    reply: str = ""


class Setting(BaseModel):
    """One user-editable runtime knob, declared by the agent. Ported from the
    template's user-settings battery, minus the env-var backing (in AgentOS the
    pack itself is the config surface)."""

    model_config = ConfigDict(frozen=True)

    key: str
    label: str = ""
    kind: str = "str"  # "int" | "bool" | "choice" | "str"
    default: str = ""
    help: str = ""
    choices: tuple[str, ...] = ()  # for kind == "choice"
    # False -> the value only takes effect on the next restart (metadata for a UI).
    applies_live: bool = True


class SettingsPack(BaseModel):
    """An agent's declarative allowlist of editable runtime knobs. This ships the
    schema + validation only; the durable override store and the edit UI are the
    deferred runtime (see docs/behavior-packs.md), the same way the tips/greeting
    kernel wiring is deferred."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    settings: tuple[Setting, ...] = ()


class BehaviorPacks(BaseModel):
    """An agent's full set of packs; every field defaults to disabled/empty."""

    model_config = ConfigDict(frozen=True)

    tips: TipsPack = TipsPack()
    greeting: GreetingPack = GreetingPack()
    help: HelpPack = HelpPack()
    settings: SettingsPack = SettingsPack()

    @classmethod
    def from_config(cls, data: Mapping[str, Any] | None) -> BehaviorPacks:
        """Parse the JSON stored on an agent row; ``None``/empty -> all-off."""
        if not data:
            return cls()
        return cls.model_validate(data)


def _normalize(text: str) -> str:
    """Casefold, strip accents/punctuation/emoji, collapse whitespace."""
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = _NON_WORD.sub(" ", folded.lower())
    return _WHITESPACE.sub(" ", folded).strip()


def _deelongate(norm: str) -> str:
    """Collapse 3+ repeated chars to one: "hiiii" -> "hi", "sooo" -> "so"."""
    return _RUN.sub(r"\1", norm)


def _pick(seq: Sequence[str], seed: str, salt: str = "") -> str:
    """Deterministically pick one item, rotated by ``seed`` (``salt`` lets two
    lists vary independently off one seed). Empty ``seed`` -> the first item."""
    if not seq:
        return ""
    if not seed:
        return seq[0]
    return seq[zlib.crc32(f"{salt}{seed}".encode()) % len(seq)]


def _matches_bare(phrases: Sequence[str], text: str) -> bool:
    """True if ``text`` is a *bare* utterance of one of ``phrases`` -- the phrase
    alone, or the phrase then only filler ("hey there team"). A phrase glued to a
    real request ("hi show me the report") is not bare and returns False, so it
    falls through to the model. Deterministic; the shared core of the greeting and
    help matchers."""
    norm = _normalize(text)
    if not norm:
        return False
    tokens = norm.split()
    squeezed = _deelongate(norm).split()
    for raw_phrase in phrases:
        phrase = _normalize(raw_phrase)
        if not phrase:
            continue
        ptoks = phrase.split()
        n = len(ptoks)
        # Try the elongation-collapsed tokens too so "hiiii" matches "hi".
        for candidate in (tokens, squeezed):
            if candidate[:n] == ptoks and all(t in _FILLER for t in candidate[n:]):
                return True
    return False


def match_greeting(packs: BehaviorPacks, text: str) -> str | None:
    """The canned reply if ``text`` is a bare greeting for this agent, else None.
    Deterministic; never calls the model."""
    pack = packs.greeting
    if not pack.enabled or not pack.reply:
        return None
    return pack.reply if _matches_bare(pack.phrases, text) else None


def match_help(packs: BehaviorPacks, text: str) -> str | None:
    """The canned reply if ``text`` is a bare help request for this agent, else
    None. Deterministic; never calls the model."""
    pack = packs.help
    if not pack.enabled or not pack.reply:
        return None
    return pack.reply if _matches_bare(pack.phrases, text) else None


def sample_tip(packs: BehaviorPacks, seed: str) -> str | None:
    """The sampled "working..." acknowledgment for this agent, or None if the
    tips pack is disabled or has no content. ``seed`` (e.g. the inbound message
    ts) makes the choice vary per message while staying reproducible."""
    pack = packs.tips
    if not pack.enabled:
        return None
    working = _pick(pack.working_lines, seed, salt="w:")
    tip = _pick(pack.tips, seed, salt="t:")
    if working and tip:
        return f"{working}\n\nTip: {tip}"
    if working:
        return working
    if tip:
        return f"Tip: {tip}"
    return None


_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


class SettingError(ValueError):
    """A user-facing validation message for a rejected setting value."""


def coerce_setting(setting: Setting, raw: str) -> str:
    """Validate + normalize a raw string to its stored form for ``setting``.
    Raises ``SettingError`` with a user-facing message on bad input. Pure; the
    platform owns this so an agent only supplies the declarative Setting."""
    value = (raw or "").strip()
    if setting.kind == "int":
        try:
            n = int(value)
        except ValueError:
            raise SettingError("must be a whole number") from None
        if n < 1:
            raise SettingError("must be 1 or more")
        return str(n)
    if setting.kind == "bool":
        low = value.lower()
        if low in _TRUTHY:
            return "true"
        if low in _FALSY:
            return "false"
        raise SettingError("use on or off")
    if setting.kind == "choice":
        if value not in setting.choices:
            raise SettingError(f"choose one of: {', '.join(setting.choices)}")
        return value
    if not value:
        raise SettingError("cannot be empty")
    return value


def resolve_settings(packs: BehaviorPacks, overrides: Mapping[str, str]) -> dict[str, str]:
    """The effective value per declared setting: a valid override wins, else the
    default. Unknown override keys and values that fail validation are ignored so
    a stale or corrupt store can never break resolution. Returns {} when the pack
    is disabled. This is the function the deferred override store / edit UI will
    call; shipping it now makes the schema usable and testable."""
    pack = packs.settings
    if not pack.enabled:
        return {}
    resolved: dict[str, str] = {}
    for setting in pack.settings:
        raw = overrides.get(setting.key)
        if raw is not None:
            try:
                resolved[setting.key] = coerce_setting(setting, raw)
                continue
            except SettingError:
                pass  # invalid override -> fall back to the default
        # An empty/invalid default (e.g. an opt-in left blank) is kept verbatim.
        try:
            resolved[setting.key] = coerce_setting(setting, setting.default)
        except SettingError:
            resolved[setting.key] = setting.default
    return resolved
