"""Discovery-grammar refinements for the whole-tree docs-lint gate (#452).

Additive coverage for the three principled exclusions (markdown link text,
doc-relative paths, glob/placeholder patterns) and the escape-hatch extension
to path/symbol findings. Every test drives the public CLI over a copied fixture
tree, like the frozen suite. The partner tests prove the refinements did not
blunt the gate: a genuinely rotten concrete citation still fails.
"""

from __future__ import annotations

from pathlib import Path

from .conftest import RunLint, write


# --- Refinement 1: markdown link text is not a citation candidate ----------


def test_link_text_relative_path_is_not_a_citation(
    clean_repo: Path, run_lint: RunLint
) -> None:
    # The reported false positive: a nav link whose text is a code span. The
    # destination is a link checker's job, never this gate's.
    write(
        clean_repo,
        "docs/nav.md",
        "See [`../ARCHITECTURE.md`](../ARCHITECTURE.md) for the map.\n",
    )
    code, out = run_lint(clean_repo)
    assert code == 0, out


def test_link_text_concrete_missing_path_is_not_a_citation(
    clean_repo: Path, run_lint: RunLint
) -> None:
    # Link-text exclusion holds even when the text looks like a concrete,
    # nonexistent repo path: it is link text, checked by a link checker.
    write(
        clean_repo,
        "docs/navc.md",
        "Jump to [`apps/ghost/imaginary.py`](apps/ghost/imaginary.py) here.\n",
    )
    code, out = run_lint(clean_repo)
    assert code == 0, out


def test_bare_span_beside_a_link_still_checked(clean_repo: Path, run_lint: RunLint) -> None:
    # The exclusion is scoped to link text only: a bare rotten span on the same
    # line, outside the link, must still fail. Proves link tracking is not a
    # blanket line skip.
    write(
        clean_repo,
        "docs/mixed.md",
        "Link [`docs/README.md`](README.md) but bare `apps/ghost/rot.py` fails.\n",
    )
    code, out = run_lint(clean_repo)
    assert code != 0
    assert "apps/ghost/rot.py" in out
    assert "does not exist" in out.lower()


# --- Refinement 2: doc-relative paths are not repo-root citations ----------


def test_doc_relative_bare_spans_are_not_citations(
    clean_repo: Path, run_lint: RunLint
) -> None:
    # Citations resolve repo-root-relative, so a ".."/"."-anchored path is by
    # definition not one. Neither the ".." nor the "." form may fail on
    # nonexistence.
    write(
        clean_repo,
        "docs/rel.md",
        "Paths `../foo.py` and `./bar.py` are doc-relative, not citations.\n",
    )
    code, out = run_lint(clean_repo)
    assert code == 0, out


def test_doc_relative_symbol_form_is_not_a_citation(
    clean_repo: Path, run_lint: RunLint
) -> None:
    # A doc-relative path in the ``::`` shorthand is navigation too, not a
    # shorthand error.
    write(clean_repo, "docs/relsym.md", "The builder `../adapter.py::build` is relative.\n")
    code, out = run_lint(clean_repo)
    assert code == 0, out


# --- Refinement 3: glob / placeholder patterns are not citations -----------


def test_glob_and_placeholder_spans_are_not_citations(
    clean_repo: Path, run_lint: RunLint
) -> None:
    # A path part carrying "*", "<", or ">" names a shape, not a concrete file.
    write(
        clean_repo,
        "docs/glob.md",
        "Patterns `skills/<name>/SKILL.md` and `x/**/y.py` are templates.\n",
    )
    code, out = run_lint(clean_repo)
    assert code == 0, out


# --- Escape hatch extension: ignore-line covers path/symbol findings --------


def test_ignore_line_suppresses_a_nonexistent_path_and_counts_it(
    clean_repo: Path, run_lint: RunLint
) -> None:
    # A genuinely-illustrative example path (a bundle-internal walkthrough) can
    # be silenced per-line. The suppression is visible in the counted summary.
    write(
        clean_repo,
        "docs/hatch.md",
        "Example bundle path:\n\n"
        "<!-- doclint:ignore-line -->\n"
        "The plugin lives at `apps/ghost/example.py` inside the bundle.\n",
    )
    code, out = run_lint(clean_repo)
    assert code == 0, out
    assert "suppressed by the ignore-line escape hatch" in out


def test_same_path_without_ignore_line_still_fails(
    clean_repo: Path, run_lint: RunLint
) -> None:
    # Partner to the hatch test: it is the comment, not the path shape, doing
    # the suppression. Without it the identical citation fails.
    write(
        clean_repo,
        "docs/nohatch.md",
        "The plugin lives at `apps/ghost/example.py` inside the bundle.\n",
    )
    code, out = run_lint(clean_repo)
    assert code != 0
    assert "apps/ghost/example.py" in out
    assert "does not exist" in out.lower()


# --- Partner: a real rotten citation still fails after all refinements ------


def test_real_rotten_citation_still_fails(clean_repo: Path, run_lint: RunLint) -> None:
    # Concrete path, not a link, not relative, not a pattern, not ignored: the
    # refinements must not have blunted the gate for the case it exists to catch.
    write(
        clean_repo,
        "docs/rot.md",
        "The gate lives in `apps/api/src/agentos_api/ghost.py`.\n",
    )
    code, out = run_lint(clean_repo)
    assert code != 0
    assert "apps/api/src/agentos_api/ghost.py" in out
    assert "does not exist" in out.lower()
