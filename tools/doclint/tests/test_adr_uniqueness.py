"""ADR number prefixes are unique across ``docs/adr/`` (#541, AC A).

Three collisions live on main today (0029, 0038, 0039) and PR #287 is brewing a
fourth off-main (0020). With no index and no gate, "the next free number" is an
`ls` against whatever branch you happen to be on, so collisions are the default
outcome rather than an accident.

Note what these tests do NOT depend on: ``docs/adr/`` is excluded from the
citation walk (ADRs are immutable, so their historical citations are allowed to
rot), and the fixture's ADR deliberately carries a rotten coordinate. The
uniqueness check reads filenames, so it must fire on a directory whose contents
the linter otherwise refuses to read.
"""

from __future__ import annotations

from pathlib import Path

from .conftest import Regenerate, RunLint, write

_BODY = "# ADR (fixture)\n\nStatus: Accepted\n"


def test_duplicate_adr_prefix_fails(clean_repo: Path, run_lint: RunLint) -> None:
    # Two ADRs claiming 0002, the shape of all three live collisions. Both file
    # names must be named: the whole remedy is choosing which one moves, and a
    # report saying only "0002 is duplicated" does not tell you that.
    write(clean_repo, "docs/adr/0002-first-decision.md", _BODY)
    write(clean_repo, "docs/adr/0002-second-decision.md", _BODY)
    code, out = run_lint(clean_repo)
    assert code != 0
    assert "0002-first-decision.md" in out
    assert "0002-second-decision.md" in out


def test_unique_adr_prefixes_pass(
    clean_repo: Path, regenerate: Regenerate, run_lint: RunLint
) -> None:
    # False-positive guard: distinct prefixes beside the fixture's 0001 stay
    # silent. A check that flags every ADR directory blocks every PR.
    #
    # The index is regenerated first because these two new ADRs belong in it --
    # adding an ADR and leaving the index stale is real drift, and the linter
    # rightly says so. That finding is another test's subject, not this one's.
    write(clean_repo, "docs/adr/0002-second-decision.md", _BODY)
    write(clean_repo, "docs/adr/0003-third-decision.md", _BODY)
    regenerate(clean_repo)
    code, out = run_lint(clean_repo)
    assert code == 0, out
