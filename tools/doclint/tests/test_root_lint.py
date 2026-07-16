"""Repo-root ``*.md`` is inside the linted root (#541, AC A).

The whole defect this ticket exists to close is one line: the walk started at
``repo_root / "docs"``, so ``ARCHITECTURE.md`` -- the doc ``README.md`` and
``llms.txt`` point release readers at -- was structurally unreachable by the
gate, and roughly thirty citations rotted there unobserved.

Widening is scoped to an explicit allowlist rather than a bare root glob: AC A
asks for "at minimum ARCHITECTURE.md", and globbing would drag five unaudited
root docs (``README.md`` alone is 24.5K) into this PR as a surprise. Each
further root doc should be a deliberate, separately-reviewed entry.

Every test drives the public CLI over a copied fixture tree, asserting through
exit code and message text only.
"""

from __future__ import annotations

from pathlib import Path

from .conftest import RunLint, write


def test_root_md_line_citation_fails(clean_repo: Path, run_lint: RunLint) -> None:
    # THE test. A line-number coordinate in the repo-root doc must be a finding.
    # Revert the widening and this goes green while the doc rots -- which is
    # precisely the state #541 was filed to describe.
    write(
        clean_repo,
        "ARCHITECTURE.md",
        "The gate lives at `runner/src/agentos_runner/approval.py:12`.\n",
    )
    code, out = run_lint(clean_repo)
    assert code != 0
    assert "ARCHITECTURE.md" in out
    assert "runner/src/agentos_runner/approval.py:12" in out


def test_root_md_missing_path_fails(clean_repo: Path, run_lint: RunLint) -> None:
    # Path validation reaches the root doc: a citation to a file that does not
    # exist (renamed, deleted, or never written) fails there like anywhere else.
    write(
        clean_repo,
        "ARCHITECTURE.md",
        "Bundles are stored by `apps/api/src/agentos_api/ghost.py`.\n",
    )
    code, out = run_lint(clean_repo)
    assert code != 0
    assert "apps/api/src/agentos_api/ghost.py" in out
    assert "does not exist" in out.lower()


def test_root_md_unresolvable_symbol_fails(clean_repo: Path, run_lint: RunLint) -> None:
    # Symbol resolution reaches the root doc too. The path is real, so only a
    # genuine `ast` parse of the cited module can catch this one -- proving the
    # root doc gets the full citation pipeline, not a cheaper path-only pass.
    write(
        clean_repo,
        "ARCHITECTURE.md",
        "Approval runs through `runner/src/agentos_runner/approval.py::no_such_fn`.\n",
    )
    code, out = run_lint(clean_repo)
    assert code != 0
    assert "no_such_fn" in out
    assert "does not resolve" in out


def test_root_md_symbol_anchor_passes(clean_repo: Path, run_lint: RunLint) -> None:
    # False-positive guard: widening the root must not flag correct citations.
    # A real symbol anchor in the root doc stays silent -- otherwise the gate is
    # noise and the next author reaches for an ignore-line comment.
    write(
        clean_repo,
        "ARCHITECTURE.md",
        "The gate is `runner/src/agentos_runner/approval.py::authorize_approval`,\n"
        "built on `runner/src/agentos_runner/approval.py::ApprovalGate`.\n",
    )
    code, out = run_lint(clean_repo)
    assert code == 0, out


def test_nested_dir_md_not_linted(clean_repo: Path, run_lint: RunLint) -> None:
    # Scoping proof: the widening reaches named repo-root docs, NOT the whole
    # tree. A markdown file in some unrelated directory stays outside the gate,
    # so the allowlist cannot quietly become `rglob("*.md")` over the repo.
    write(
        clean_repo,
        "some_other_dir/notes.md",
        "Scratch notes citing `apps/api/src/agentos_api/ghost.py:99`.\n",
    )
    code, out = run_lint(clean_repo)
    assert code == 0, out
