"""Classify a tool call as idempotent (safe) or side-effecting.

The ACI ``side_effect_flag`` gates the no-retry-after-side-effects rule (ACI
section 2b): a failed run that executed a non-idempotent tool escalates to a
human instead of retrying, because at-least-once delivery plus a mutating tool
means a blind retry risks a duplicate real-world action.

Design choice (documented for F1/K1): a **read-only allowlist**, deny-by-default.
A set of known read-only tools is treated as idempotent; every other tool the
harness executes is treated as potentially side-effecting and flags the run.
This fails safe: a new or unknown tool escalates rather than being silently
retried. The alternative, an explicit non-idempotent denylist, fails open (a new
mutating tool would be missed) and was rejected for that reason.

The read-only set is **declared per harness adapter**, because each harness names
its built-in tools differently (Claude Code uses PascalCase, OpenCode uses
lowercase ids), so a single hardcoded set would misclassify one harness's tools
as side-effecting. See ``CLAUDE_READONLY_TOOLS`` in ``adapter.py`` and
``OPENCODE_READONLY_TOOLS`` in ``opencode/session.py``; the classifier itself is
harness-agnostic and just takes the declared set. The declaration is overridable
via ``AGENTOS_IDEMPOTENT_TOOLS`` (a comma separated list that replaces the
harness declaration) so an operator can widen it per plugin without a code
change.
"""

from collections.abc import Iterable


class SideEffectClassifier:
    """Decide whether a tool name denotes a non-idempotent (side-effecting) call."""

    def __init__(self, idempotent_tools: Iterable[str]) -> None:
        self._idempotent = frozenset(idempotent_tools)

    def is_side_effecting(self, tool_name: str) -> bool:
        """True if executing ``tool_name`` may cause a non-idempotent side effect."""

        return tool_name not in self._idempotent
