"""Citation discovery, classification, and the raw line-ban rule.

All tests drive the public CLI over a copied fixture tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentos_doclint import SOURCE_EXTENSIONS

from .conftest import RunLint, write


# --- Test 1: a cited path that does not exist ------------------------------


def test_nonexistent_path_fails(clean_repo: Path, run_lint: RunLint) -> None:
    write(
        clean_repo,
        "docs/ghost.md",
        "The gate lives in `apps/api/src/agentos_api/ghost.py`.\n",
    )
    code, out = run_lint(clean_repo)
    assert code != 0
    assert "docs/ghost.md" in out  # names the offending doc
    assert "apps/api/src/agentos_api/ghost.py" in out  # names the citation
    assert "does not exist" in out.lower()  # names the reason


# --- Test 3: the unified line-citation ban ---------------------------------


def test_line_number_citation_fails(clean_repo: Path, run_lint: RunLint) -> None:
    write(clean_repo, "docs/line.md", "See `queue.py:60` for the detail.\n")
    code, out = run_lint(clean_repo)
    assert code != 0
    assert "docs/line.md" in out
    assert "queue.py:60" in out


@pytest.mark.parametrize("ext", SOURCE_EXTENSIONS)
def test_line_ban_covers_every_recognized_extension(
    clean_repo: Path, run_lint: RunLint, ext: str
) -> None:
    # Parametrized over the tool's own extension constant so the test list
    # cannot drift from the tool. The .rs and .md cases mirror live
    # violations; the rest are the forward-looking half.
    write(clean_repo, "docs/ext.md", f"Coordinate `pkg/x.{ext}:12` is rotten.\n")
    code, out = run_lint(clean_repo)
    assert code != 0
    assert f"pkg/x.{ext}:12" in out


def test_line_ban_covers_github_hash_L_form(clean_repo: Path, run_lint: RunLint) -> None:
    # Zero instances exist today; this is purely preventive. Both spellings of
    # the same rotten coordinate must fail.
    write(
        clean_repo,
        "docs/hashl.md",
        "GitHub links `path.py#L60` and `cli/src/queue.rs#L35` are banned too.\n",
    )
    code, out = run_lint(clean_repo)
    assert code != 0
    assert "path.py#L60" in out
    assert "cli/src/queue.rs#L35" in out


# --- Test 6: shorthand ::symbol with no path is a hard error ---------------


def test_shorthand_symbol_citation_without_path_is_hard_error(
    clean_repo: Path, run_lint: RunLint
) -> None:
    write(clean_repo, "docs/short.md", "The builder is `adapter.py::build_options`.\n")
    code, out = run_lint(clean_repo)
    assert code != 0
    assert "adapter.py::build_options" in out  # names the citation
    assert "full repo-relative path" in out.lower()  # instructs the fix


def test_shorthand_partner_full_path_passes(clean_repo: Path, run_lint: RunLint) -> None:
    write(
        clean_repo,
        "docs/full.md",
        "The builder is `runner/src/agentos_runner/approval.py::build_options`.\n",
    )
    code, _ = run_lint(clean_repo)
    assert code == 0


def test_shorthand_partner_prose_spans_pass(clean_repo: Path, run_lint: RunLint) -> None:
    write(clean_repo, "docs/prose.md", "Fields `payload` and `ok: false` are fine.\n")
    code, _ = run_lint(clean_repo)
    assert code == 0


def test_shorthand_partner_bare_filename_is_not_an_error(
    clean_repo: Path, run_lint: RunLint
) -> None:
    # Extension, no ::, no / -> prose shorthand, not a false claim of being
    # gated. Only the :: makes it masquerade as checked.
    write(clean_repo, "docs/bare.md", "Look in `adapter.py` for the builder.\n")
    code, _ = run_lint(clean_repo)
    assert code == 0


# --- Test 8: fenced blocks ------------------------------------------------


def test_fenced_code_block_line_citation_is_not_a_citation_but_still_fails_raw_rule(
    clean_repo: Path, run_lint: RunLint
) -> None:
    # A path inside a fenced block is NOT path-checked: a fictional,
    # nonexistent path in an example must not fail.
    write(
        clean_repo,
        "docs/fenced_ok.md",
        "Example:\n\n```\ncite `apps/ghost/imaginary.py` like this\n```\n",
    )
    code, _ = run_lint(clean_repo)
    assert code == 0

    # But a line coordinate anywhere, fenced or not, DOES fail the raw rule.
    write(
        clean_repo,
        "docs/fenced_bad.md",
        "Example:\n\n```\nsee queue.py:60 here\n```\n",
    )
    code, out = run_lint(clean_repo)
    assert code != 0
    assert "queue.py:60" in out


# --- Test 9: ordinary prose spans are not citations ------------------------


def test_prose_code_span_is_not_a_citation(clean_repo: Path, run_lint: RunLint) -> None:
    write(
        clean_repo,
        "docs/spans.md",
        "Spans `payload`, `ok: false`, `SET NX`, `bundle-format` are not citations.\n",
    )
    code, _ = run_lint(clean_repo)
    assert code == 0


# --- Test 10: docs/adr/ is excluded from the linted root -------------------


def test_adr_directory_is_not_linted(clean_repo: Path, run_lint: RunLint) -> None:
    write(
        clean_repo,
        "docs/adr/0099-historical.md",
        "Historical `queue.py:60` and `apps/gone/missing.py` stay untouched.\n",
    )
    code, _ = run_lint(clean_repo)
    assert code == 0
