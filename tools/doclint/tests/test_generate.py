"""Generation: drift detection, markers, and stable order.

Uses the public ``render_index_table`` generator surface plus the CLI.
"""

from __future__ import annotations

from pathlib import Path

from curie_doclint import render_index_table

from .conftest import RunLint, write

_BEGIN = "<!-- BEGIN GENERATED: seam-table (curie dev docs-lint) -->"
_END = "<!-- END GENERATED: seam-table -->"


def _between_markers(text: str) -> str:
    return text.split(_BEGIN, 1)[1].split(_END, 1)[0]


# --- Test 4: a drifted index fails; the generator round-trips --------------


def test_drifted_index_fails(clean_repo: Path, run_lint: RunLint) -> None:
    index = clean_repo / "docs" / "interfaces.md"
    text = index.read_text(encoding="utf-8")
    # The seam doc declares CLEAN | 2; drift the on-disk row to NONE | 0.
    drifted = text.replace(
        "| Substrate | CLEAN | 2 | A |",
        "| Substrate | NONE | 0 | A |",
    )
    assert drifted != text
    index.write_text(drifted, encoding="utf-8")
    code, _ = run_lint(clean_repo)
    assert code != 0


def test_regenerated_index_matches_frontmatter(clean_repo: Path) -> None:
    # The generator round-trips the clean fixture to the bytes already on disk.
    on_disk = _between_markers(
        (clean_repo / "docs" / "interfaces.md").read_text(encoding="utf-8")
    )
    rendered = render_index_table(clean_repo)
    assert rendered.strip() == on_disk.strip()


# --- Test 7 partner: a new seam dir appears in the generated table ---------


def test_new_seam_dir_appears_in_generated_table(clean_repo: Path) -> None:
    fm = (
        "---\n"
        "seam: New Seam\n"
        "kind: CLEAN\n"
        "impls: 1\n"
        "grade: C\n"
        "epics:\n"
        '  - "#999"\n'
        "order: 3\n"
        "---\n"
        "\n# New seam\n\nProse.\n"
    )
    write(clean_repo, "docs/interfaces/newseam/INTERFACE.md", fm)
    rendered = render_index_table(clean_repo)
    # The glob is live: no code change, and the new dir shows up as a row.
    assert "New Seam" in rendered
    assert "interfaces/newseam/INTERFACE.md" in rendered


# --- Test 11: a missing/unpaired marker is a hard error --------------------


def test_missing_marker_is_a_hard_error(clean_repo: Path, run_lint: RunLint) -> None:
    index = clean_repo / "docs" / "interfaces.md"
    text = index.read_text(encoding="utf-8")
    # Remove the END marker, leaving BEGIN unpaired.
    index.write_text(text.replace(_END + "\n", ""), encoding="utf-8")
    code, out = run_lint(clean_repo)
    assert code != 0
    assert "marker" in out.lower()


# --- Test 13: stable order, declared order beats filesystem/alpha order -----


def test_table_row_order_is_stable(clean_repo: Path) -> None:
    first = render_index_table(clean_repo)
    second = render_index_table(clean_repo)
    assert first == second  # byte-identical across runs

    # "approval" sorts before "substrate" on disk, but substrate declares
    # order 1 and approval order 2, so the declared order must win.
    assert first.index("Substrate") < first.index("Approval")
