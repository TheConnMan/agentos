"""Dataclass-field-enumeration match (#615).

A sealed parenthetical field list of the form
`` `path::Class`: `a`, `b`, ...) `` must equal the cited class's actual
annotated fields. #573 deleted ``ClaimView.labels`` from the dataclass and both
adapters but left the seam doc's field list intact; the citation resolved, so
citation resolution alone never caught the stale field. This complements the
link-text-vs-target gate by catching the other described-shape drift. These
tests drive the public CLI over a copied fixture tree, asserting through exit
code and message text only.
"""

from __future__ import annotations

from pathlib import Path

from .conftest import RunLint, write

DATA = "runner/src/curie_runner/approval.py::SomeData"


def test_extra_field_fails(clean_repo: Path, run_lint: RunLint) -> None:
    write(
        clean_repo,
        "docs/fields_extra.md",
        f"The payload is `{DATA}` (`{DATA}`: `a`, `b`, `ghost`).\n",
    )
    code, out = run_lint(clean_repo)
    assert code != 0
    assert "ghost" in out
    assert "does not match" in out


def test_missing_field_fails(clean_repo: Path, run_lint: RunLint) -> None:
    write(
        clean_repo,
        "docs/fields_missing.md",
        f"The payload is `{DATA}` (`{DATA}`: `a`).\n",
    )
    code, out = run_lint(clean_repo)
    assert code != 0
    assert "does not match" in out
    assert "b" in out


def test_exact_match_passes(clean_repo: Path, run_lint: RunLint) -> None:
    write(
        clean_repo,
        "docs/fields_ok.md",
        f"The payload is `{DATA}` (`{DATA}`: `a`, `b`).\n",
    )
    code, out = run_lint(clean_repo)
    assert code == 0, out


def test_reordered_fields_pass(clean_repo: Path, run_lint: RunLint) -> None:
    # Set comparison: order is allowed to differ.
    write(
        clean_repo,
        "docs/fields_reordered.md",
        f"The payload is `{DATA}` (`{DATA}`: `b`, `a`).\n",
    )
    code, out = run_lint(clean_repo)
    assert code == 0, out


def test_non_sealed_prose_does_not_fire(clean_repo: Path, run_lint: RunLint) -> None:
    # A prose sentence that mentions a field or two is not a sealed enumeration.
    write(
        clean_repo,
        "docs/fields_prose.md",
        f"The `{DATA}` type carries `a` and other things.\n",
    )
    code, out = run_lint(clean_repo)
    assert code == 0, out


def test_class_without_annotated_fields_is_skipped(clean_repo: Path, run_lint: RunLint) -> None:
    # ApprovalGate has methods but no annotated fields; a listed "field" must not
    # produce a mismatch finding (there is nothing to compare against).
    cls = "runner/src/curie_runner/approval.py::ApprovalGate"
    write(
        clean_repo,
        "docs/fields_methods.md",
        f"The gate is `{cls}` (`{cls}`: `consume_grant`).\n",
    )
    code, out = run_lint(clean_repo)
    assert code == 0, out
