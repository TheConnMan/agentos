"""Per-run budget accounting for the runner.

The ACI budget (``AGENTOS_BUDGET``) carries ``max_output_tokens_per_run`` and
``max_usd_per_day``. This module enforces the per-run **output token ceiling**:
the runner accumulates output tokens as the SDK reports usage and halts the turn
with a classified-failure final once the ceiling is crossed. The daily USD cap is
handed to the SDK natively (``ClaudeAgentOptions.max_budget_usd``); it is a
process/session ceiling the harness enforces, not a per-turn concern here.

Enforcement granularity is the SDK loop boundary: usage is read from each message
the SDK yields (assistant messages when they carry usage, and always the terminal
result), so the halt lands at the first boundary where cumulative output crosses
the ceiling, not mid-token. That is the tightest guarantee the SDK's usage
reporting allows and is sufficient for the product behavior (bounding spend, not
byte-exact truncation).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# The classification string carried on the error event and used by consumers
# (F1's retry rules) to recognise a budget halt versus a model failure.
BUDGET_CLASSIFICATION = "budget-exceeded"


def output_tokens(usage: Mapping[str, Any] | None) -> int:
    """Extract the output-token count from an SDK usage mapping.

    Returns 0 for a missing usage block or a missing/non-integer field, so a
    turn with no usage reporting simply never trips the ceiling rather than
    erroring.
    """

    if not usage:
        return 0
    value = usage.get("output_tokens")
    return value if isinstance(value, int) else 0


@dataclass
class BudgetTracker:
    """Accumulates output tokens for one run and reports ceiling crossings."""

    ceiling: int
    used: int = 0

    def add(self, usage: Mapping[str, Any] | None) -> None:
        """Fold one message's usage into the running total (monotonic).

        SDK usage is typically cumulative for the turn, so the running total
        tracks the max seen rather than a naive sum, which would double count.
        """

        self.used = max(self.used, output_tokens(usage))

    @property
    def exceeded(self) -> bool:
        """True once accumulated output tokens exceed the ceiling.

        A non-positive ceiling disables enforcement (unbounded run).
        """

        return self.ceiling > 0 and self.used > self.ceiling
