"""The one finding type the linter emits.

A ``Finding`` carries at least the repo-relative doc path, the offending
citation or field, and a human reason, per the public contract. The printed
line combines all three so the test suite (which reads exit code and message
text only) can assert on any of them.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    """A single lint failure.

    ``doc`` is the repo-relative path of the file the failure was found in.
    ``citation`` is the offending token: a citation string, a front-matter
    field name, a kind value, a coordinate, or a generated-marker name.
    ``reason`` explains the failure. ``line`` is the 1-based line number when
    known, else ``None``.
    """

    doc: str
    citation: str
    reason: str
    line: int | None = None

    def render(self) -> str:
        """One printable line naming the doc, the citation, and the reason."""
        location = self.doc if self.line is None else f"{self.doc}:{self.line}"
        return f"{location}: {self.citation}: {self.reason}"
