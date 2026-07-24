"""Front-matter is a contract; an unvalidated contract is prose.

Also pins that the generator globs ``docs/interfaces/*/INTERFACE.md`` rather
than iterating a hardcoded list of seams.
"""

from __future__ import annotations

from pathlib import Path

from .conftest import RunLint, write

# A minimal seam body reused below. The generated header intentionally matches
# a CLEAN/2/A front-matter so that only the front-matter defect under test is
# the reason a run fails.
_HEADER = (
    "<!-- BEGIN GENERATED: header (curie dev docs-lint) -->\n"
    "> **Kind:** CLEAN &nbsp;·&nbsp; **Implementations today:** 2"
    " &nbsp;·&nbsp; **Swap-readiness grade:** A\n"
    "<!-- END GENERATED: header -->\n"
)


def _seam(front_matter: str) -> str:
    return f"{front_matter}\n# Seam\n\n{_HEADER}\nProse.\n"


# --- Test 12: front-matter validation --------------------------------------


def test_frontmatter_missing_required_field_fails(
    clean_repo: Path, run_lint: RunLint
) -> None:
    # substrate front-matter with no `grade`.
    fm = (
        "---\n"
        "seam: Substrate\n"
        "kind: CLEAN\n"
        "impls: 2\n"
        "epics:\n"
        '  - "#86"\n'
        "order: 1\n"
        "---\n"
    )
    write(clean_repo, "docs/interfaces/substrate/INTERFACE.md", _seam(fm))
    code, out = run_lint(clean_repo)
    assert code != 0
    assert "grade" in out.lower()
    assert "required" in out.lower()


def test_frontmatter_invalid_kind_fails(clean_repo: Path, run_lint: RunLint) -> None:
    fm = (
        "---\n"
        "seam: Substrate\n"
        "kind: MAYBE\n"
        "impls: 2\n"
        "grade: A\n"
        "epics:\n"
        '  - "#86"\n'
        "order: 1\n"
        "---\n"
    )
    write(clean_repo, "docs/interfaces/substrate/INTERFACE.md", _seam(fm))
    code, out = run_lint(clean_repo)
    assert code != 0
    assert "kind" in out.lower()
    assert "MAYBE" in out


# --- Test 7: a seam doc without front-matter is a hard error ---------------


def test_seam_doc_without_frontmatter_is_hard_error(
    clean_repo: Path, run_lint: RunLint
) -> None:
    # Pins that the generator globs the interfaces dir: a new seam with no
    # front-matter must be caught, not silently skipped.
    write(
        clean_repo,
        "docs/interfaces/newseam/INTERFACE.md",
        "# New seam\n\nNo front-matter block at all.\n",
    )
    code, out = run_lint(clean_repo)
    assert code != 0
    assert "docs/interfaces/newseam/INTERFACE.md" in out
