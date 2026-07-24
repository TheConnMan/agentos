"""A graded seam's ``grade:`` must agree with the vision table (#541, AC A).

Each seam doc names ``docs/architecture-vision.md``'s swap-readiness table as
its authority, but nothing checked that claim, so ``blob-storage`` sat at ``A-``
against the table's ``B+`` indefinitely. This check makes the table machine-
checked authority instead of prose a doc happens to cite.

The mapping is an explicit ``vision_row:`` front-matter key, not a fuzzy name
match. Seven seams carry a grade; the table has six rows, because
``aci-producer`` and ``harness-modelsession`` both map to "Harness / runtime".
A name-match would be a latent false positive that fires the first time those
two grades diverge -- they agree today purely by luck.

Tests mutate the vision table rather than front-matter wherever possible: the
grade also feeds the generated header and index regions, so editing front-matter
directly trips the pre-existing drift check and a test would go red for the
wrong reason. Where front-matter must change, the real generator regenerates
those regions first, exactly as ``scripts/check-docs.sh`` does.
"""

from __future__ import annotations

from pathlib import Path

from curie_doclint import main

from .conftest import RunLint

_VISION_REL = "docs/architecture-vision.md"


def _set_vision_grade(repo: Path, job: str, cell: str) -> None:
    """Rewrite one row's Grade cell in the swap-readiness table."""
    path = repo / _VISION_REL
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.startswith(f"| {job} |"):
            columns = line.split("|")
            columns[4] = f" {cell} "
            lines[index] = "|".join(columns)
            break
    else:  # pragma: no cover - a typo'd job name must not pass silently
        raise AssertionError(f"no {job!r} row in {_VISION_REL}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _set_front_matter(repo: Path, seam: str, key: str, value: str | None) -> None:
    """Set or drop a front-matter key on a seam doc, then regenerate.

    The regeneration is the real ``--write`` pass, so the generated header and
    index regions stay consistent and any resulting finding is about the grade
    check under test, not about drift the mutation itself introduced.
    """
    path = repo / "docs/interfaces" / seam / "INTERFACE.md"
    lines = path.read_text(encoding="utf-8").splitlines()
    kept = [line for line in lines if not line.startswith(f"{key}:")]
    if value is not None:
        kept.insert(1, f"{key}: {value}")
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    main(["--repo-root", str(repo), "--write"])


def test_grade_agreement_passes(clean_repo: Path, run_lint: RunLint) -> None:
    # False-positive guard: the clean fixture's two graded seams agree with
    # their vision rows and must stay silent.
    code, out = run_lint(clean_repo)
    assert code == 0, out


def test_grade_disagreement_fails(clean_repo: Path, run_lint: RunLint) -> None:
    # The blob-storage defect in miniature: front-matter says B+, the table it
    # names as its authority says D+. The report must name the seam and both
    # grades, or the reader cannot tell which side is wrong.
    #
    # The Approval seam is used deliberately: its grades are two-character
    # tokens. Substrate's bare "A" would make `"A" in out` true for almost any
    # output, so the assert would pass on a report that named neither grade.
    _set_vision_grade(clean_repo, "Approval", "D+")
    code, out = run_lint(clean_repo)
    assert code != 0
    assert "Approval" in out
    assert "B+" in out
    assert "D+" in out


def test_grade_rationale_suffix_ignored(clean_repo: Path, run_lint: RunLint) -> None:
    # The real table writes "A-: strongest seam in the system; docked for ..."
    # -- a grade token, a colon, then prose. The check compares the token before
    # the colon, never the whole cell. Compare the cell and every real row fails.
    _set_vision_grade(
        clean_repo,
        "Substrate",
        "A: two real implementations behind one port; docked for nothing yet",
    )
    code, out = run_lint(clean_repo)
    assert code == 0, out


def test_ungraded_seam_skipped(clean_repo: Path, run_lint: RunLint) -> None:
    # Eleven of seventeen seams read `grade: not separately graded`. They have
    # no vision row by design and must not be dragged into the check -- that
    # would make the gate unpassable for the majority of the catalog.
    _set_front_matter(clean_repo, "substrate", "grade", "not separately graded")
    _set_front_matter(clean_repo, "substrate", "vision_row", None)
    code, out = run_lint(clean_repo)
    assert code == 0, out


def test_graded_seam_absent_from_vision_table_is_explicit(
    clean_repo: Path, run_lint: RunLint
) -> None:
    # The aci-producer hazard, and the most important test in this file. A
    # graded seam pointing at a row the table does not contain is a FAILURE, not
    # a skip. Skipping is the latent false negative: the seam looks checked, the
    # gate is green, and the grade rots exactly as blob-storage's did. A typo'd
    # or deleted row must surface, not silently disable the check for that seam.
    _set_front_matter(clean_repo, "substrate", "vision_row", "No Such Row")
    code, out = run_lint(clean_repo)
    assert code != 0
    assert "Substrate" in out or "substrate" in out
    assert "No Such Row" in out


def test_graded_seam_missing_vision_row_fails(clean_repo: Path, run_lint: RunLint) -> None:
    # Partner to the test above: a grade with no declared authority at all is
    # equally unverifiable, so adding the eighth graded seam cannot be done by
    # omitting the key. Skip here and the check is opt-in, which is no gate.
    _set_front_matter(clean_repo, "substrate", "vision_row", None)
    code, out = run_lint(clean_repo)
    assert code != 0
    assert "Substrate" in out or "substrate" in out
    assert "vision_row" in out


def test_shared_vision_row_requires_both_seams_to_match(
    clean_repo: Path, run_lint: RunLint
) -> None:
    # Two seams legitimately share one row (aci-producer and
    # harness-modelsession both map to "Harness / runtime"), and the row is the
    # authority for BOTH. Here Approval (B+) joins Substrate's row (A) and must
    # fail. Check only the first seam per row and the second grade is unguarded.
    _set_front_matter(clean_repo, "approval", "vision_row", "Substrate")
    code, out = run_lint(clean_repo)
    assert code != 0
    assert "Approval" in out or "approval" in out
