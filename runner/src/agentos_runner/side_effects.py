"""Classify a tool call as idempotent (safe) or side-effecting.

The ACI ``side_effect_flag`` gates the no-retry-after-side-effects rule (ACI
section 2b): a failed run that executed a non-idempotent tool escalates to a
human instead of retrying, because at-least-once delivery plus a mutating tool
means a blind retry risks a duplicate real-world action.

Design choice (documented for F1/K1): a **read-only allowlist**, deny-by-default.
A small set of known read-only Claude Code tools is treated as idempotent; every
other tool the harness executes is treated as potentially side-effecting and
flags the run. This fails safe: a new or unknown tool escalates rather than
being silently retried. The alternative, an explicit non-idempotent denylist,
fails open (a new mutating tool would be missed) and was rejected for that
reason. The allowlist is overridable via ``AGENTOS_IDEMPOTENT_TOOLS`` (a comma
separated list that replaces the default) so an operator can widen it per plugin
without a code change.
"""

from collections.abc import Iterable

# Built-in Claude Code tools that only read state. Names match the SDK's tool
# identifiers. Anything not listed here is treated as side-effecting.
DEFAULT_IDEMPOTENT_TOOLS: frozenset[str] = frozenset(
    {
        "Read",
        "Glob",
        "Grep",
        "LS",
        "NotebookRead",
        "WebFetch",
        "WebSearch",
        "TodoRead",
    }
)


class SideEffectClassifier:
    """Decide whether a tool name denotes a non-idempotent (side-effecting) call."""

    def __init__(self, idempotent_tools: Iterable[str] | None = None) -> None:
        self._idempotent = (
            frozenset(idempotent_tools)
            if idempotent_tools is not None
            else DEFAULT_IDEMPOTENT_TOOLS
        )

    def is_side_effecting(self, tool_name: str) -> bool:
        """True if executing ``tool_name`` may cause a non-idempotent side effect."""

        return tool_name not in self._idempotent
