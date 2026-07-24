"""Fixture module with a known, stable set of resolvable symbols.

The doclint symbol resolver parses this file with ``ast`` (never imports it),
so the bindings below are the ground truth the resolution tests cite against.
"""

from dataclasses import dataclass

from ._helpers import build_options  # ImportFrom-bound name, resolvable at this site

GRANT_PREFIX = "curie:grant"  # module-level constant (assignment target)


@dataclass(frozen=True)
class SomeData:
    """Field-bearing dataclass, ground truth for the field-enumeration check."""

    a: str
    b: int


def authorize_approval(actor: str) -> bool:
    """Module-level function symbol."""
    return bool(actor)


class ApprovalGate:
    """Class symbol."""

    def consume_grant(self, tool: str) -> None:
        """Method symbol, cited as ``ApprovalGate.consume_grant``."""
        _ = tool


__all__ = ["authorize_approval", "ApprovalGate", "GRANT_PREFIX", "SomeData", "build_options"]
