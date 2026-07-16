"""Gate-integrity regressions for the whole-tree docs-lint gate (#452).

Defect 1: a code span that is the text of a markdown link used to escape
path/symbol validation entirely, so a real repo-root citation written as a link
could rot silently. The fix runs the ordinary classifier on link text and skips
only self-referential relative links (visible text identical to the link's own
destination), which is navigation, not a citation.

Every test drives the public CLI over a copied fixture tree, like the frozen
suite, asserting through exit code and message text only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentos_doclint import main

from .conftest import RunLint, write


def test_link_text_repo_path_to_missing_file_fails(
    clean_repo: Path, run_lint: RunLint
) -> None:
    # The blind spot: a real repo-root citation decorated as a link (its
    # destination differs from its text) must be validated like any other
    # citation. A nonexistent path fails; renaming or deleting the file can no
    # longer hide behind the link syntax.
    write(
        clean_repo,
        "docs/blind.md",
        "The gate lives in "
        "[`apps/api/src/agentos_api/ghost.py`](../apps/api/src/agentos_api/ghost.py).\n",
    )
    code, out = run_lint(clean_repo)
    assert code != 0
    assert "apps/api/src/agentos_api/ghost.py" in out
    assert "does not exist" in out.lower()


def test_nav_self_link_to_missing_path_still_passes(
    clean_repo: Path, run_lint: RunLint
) -> None:
    # Partner to the blind-spot test: a self-referential relative nav link (text
    # identical to its own destination) is navigation, not a citation, even when
    # the path does not resolve at the repo root. This is what keeps the real
    # tree's `[`diagrams/x.md`](diagrams/x.md)` links quiet.
    write(
        clean_repo,
        "docs/nav.md",
        "See [`../ARCHITECTURE.md`](../ARCHITECTURE.md) and "
        "[`diagrams/message-flow.md`](diagrams/message-flow.md).\n",
    )
    code, out = run_lint(clean_repo)
    assert code == 0, out


def test_inline_ignore_suppresses_only_its_own_line(
    clean_repo: Path, run_lint: RunLint
) -> None:
    # Per-line-granularity proof: two citations to missing paths sit on adjacent
    # physical lines of ONE paragraph. An inline `<!-- doclint:ignore-line -->`
    # at the end of the first line silences only that line; the bare citation on
    # the next line of the same paragraph still fails. The old paragraph-wide
    # behavior stamped both spans on the paragraph's first line, so one comment
    # would have masked both -- exactly the gate hole this change closes.
    write(
        clean_repo,
        "docs/perline.md",
        "The scaffold writes `pkg/ghost/first.py` here <!-- doclint:ignore-line -->\n"
        "and also writes `pkg/ghost/second.py` there.\n",
    )
    code, out = run_lint(clean_repo)
    assert code != 0
    assert "pkg/ghost/second.py" in out
    assert "pkg/ghost/first.py" not in out


def test_finding_line_is_true_physical_line_in_paragraph(
    clean_repo: Path, run_lint: RunLint
) -> None:
    # A citation on the third line of a multi-line paragraph must be reported on
    # that physical line, not the inline container's start line. This is what
    # makes a per-line escape hatch meaningful: the reported line is where the
    # backticks actually are.
    write(
        clean_repo,
        "docs/lines.md",
        "First line of the paragraph with no citation.\n"
        "Second line also clean, still no citation here.\n"
        "Third line cites `pkg/ghost/third.py` which is missing.\n",
    )
    code, out = run_lint(clean_repo)
    assert code != 0
    assert "docs/lines.md:3:" in out
    assert "pkg/ghost/third.py" in out


def test_deleted_adr_index_fails(clean_repo: Path, run_lint: RunLint) -> None:
    # Defect 2: the gate verified what was written and never what was missing.
    # A commit that deletes the ADR index used to sail through -- the check
    # returned no finding for an absent file, and `scripts/check-docs.sh` diffs
    # the working tree against the current commit, so the deletion was already
    # "committed" by the time the gate looked. That is the same defect species
    # this ticket exists to fix, and it is not cosmetic: the index's absence is
    # the direct cause of the three number collisions renumbered here. With no
    # list, "the next free number" is an `ls` against a possibly stale branch.
    (clean_repo / "docs/adr/README.md").unlink()
    code, out = run_lint(clean_repo)
    assert code != 0
    assert "docs/adr/README.md" in out
    assert "missing" in out.lower()


def test_deleted_root_doc_fails(clean_repo: Path, run_lint: RunLint) -> None:
    # The same defect as the ADR index above, at the other required artifact this
    # ticket adds to the gate. The root allowlist was walked with an `.is_file()`
    # filter, so a commit deleting ARCHITECTURE.md -- the doc README.md and
    # llms.txt point release readers at, and the reason #541 exists -- silently
    # emptied the lint list for it: zero findings, exit 0. "The gate verifies
    # what is written, never what is missing" is precisely the hole; an
    # allowlisted root doc that does not resolve must be a finding, not an
    # omission, or the widening is undone by a single `git rm`.
    (clean_repo / "ARCHITECTURE.md").unlink()
    code, out = run_lint(clean_repo)
    assert code != 0
    assert "ARCHITECTURE.md" in out
    assert "missing" in out.lower()


def test_write_mode_refuses_to_scaffold_a_deleted_adr_index(
    clean_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The write half of the same hole: `--write` skipped the absent file too, so
    # `scripts/check-docs.sh` (which runs --write, then diffs, then lints) had
    # nothing to trip over at its first step. It now reports, and deliberately
    # does NOT scaffold a replacement: a stub would silently discard the index's
    # hand-written "claiming a number" prose, papering over the deletion at the
    # one moment it should be loudest. The file must stay gone and the run must
    # fail -- restoring it is a human decision.
    index = clean_repo / "docs/adr/README.md"
    index.unlink()
    code = main(["--repo-root", str(clean_repo), "--write"])
    out = capsys.readouterr().out
    assert code != 0
    assert "docs/adr/README.md" in out
    assert not index.exists()


def test_link_text_repo_path_to_real_file_passes(
    clean_repo: Path, run_lint: RunLint
) -> None:
    # A decorated citation whose destination differs from its text and whose
    # repo-root path exists must pass: the fix validates it, it does not flag it.
    write(
        clean_repo,
        "docs/good.md",
        "The gate lives in "
        "[`runner/src/agentos_runner/approval.py`]"
        "(../runner/src/agentos_runner/approval.py).\n",
    )
    code, out = run_lint(clean_repo)
    assert code == 0, out
