"""Prompt-cache accounting over an SDK usage mapping (issue #255).

CC-style harnesses lean on prompt caching to keep a warm thread cheap: the large,
byte-stable prefix (system prompt + tools + prior turns) is cached on the first
turn and *read* — not re-billed at full rate — on every subsequent turn against
the same session. The Anthropic wire reports this as two token counts on each
turn's usage block:

* ``cache_creation_input_tokens`` — tokens written into the cache this turn (the
  cold, full-price write on the first turn that establishes the prefix).
* ``cache_read_input_tokens`` — tokens served *from* the cache this turn (the
  warm, ~10x-cheaper read that a repeated prefix should produce).

A **cache hit** is a turn with ``cache_read_input_tokens > 0``: the harness reused
a previously-cached prefix. This matters because translating gateways have
documented cache breakage — a silent no-cache at ~10x cost via OpenRouter BYOK, a
LiteLLM ``cache_control`` mis-map — that leaves a warm thread reporting zero
cache reads while still charging full price. These helpers turn that failure into
something a smoke test can assert on, rather than a silent cost blowup.

The functions are total and defensive: a missing usage block, a missing field, or
a non-integer value reads as zero, so a provider that reports no cache usage
simply looks like a cold turn rather than raising.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

CACHE_READ_KEY = "cache_read_input_tokens"
CACHE_CREATION_KEY = "cache_creation_input_tokens"


def _int_field(usage: Mapping[str, Any] | None, key: str) -> int:
    if not usage:
        return 0
    value = usage.get(key)
    # bool is an int subclass; a stray True must not read as 1 cache token.
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


def cache_read_tokens(usage: Mapping[str, Any] | None) -> int:
    """Tokens served from the prompt cache this turn (0 if none/unreported)."""
    return _int_field(usage, CACHE_READ_KEY)


def cache_creation_tokens(usage: Mapping[str, Any] | None) -> int:
    """Tokens written into the prompt cache this turn (0 if none/unreported)."""
    return _int_field(usage, CACHE_CREATION_KEY)


def is_cache_hit(usage: Mapping[str, Any] | None) -> bool:
    """True when this turn read from the prompt cache (a warm-thread cache hit).

    The load-bearing predicate the smoke test asserts: a warm turn that does not
    hit cache (a broken translating gateway, a byte-unstable prefix) returns
    False here and fails the test loudly.
    """
    return cache_read_tokens(usage) > 0
