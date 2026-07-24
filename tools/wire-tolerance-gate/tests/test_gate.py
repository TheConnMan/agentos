"""Tests for the _AciModel tolerant-decode gate (issue #625).

``fixtures/repo`` is a tiny synthetic repo shaped like the real one: a
stand-in ``_AciModel``/``Widget``/``Gadget`` hierarchy under
``packages/aci-protocol/src/aci_protocol/events.py`` and four call sites under
``apps/fake`` covering the four outcomes the gate must tell apart. The
negative-control test (``test_gate_flags_the_unsanctioned_call_site``) is the
one that proves the gate is not vacuous: it fails the suite if the gate stops
catching an unsanctioned call site.
"""

from __future__ import annotations

from pathlib import Path

from curie_wire_tolerance_gate import find_violations

_FIXTURE_REPO = Path(__file__).parent / "fixtures" / "repo"
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _fixture_violations() -> list:
    return find_violations(_FIXTURE_REPO, _FIXTURE_REPO / "allowlist.json")


def test_gate_flags_the_unsanctioned_call_site() -> None:
    """Negative control: an unsanctioned call site must be flagged, or the
    gate is vacuous."""

    flagged = {(v.path, v.symbol) for v in _fixture_violations()}
    assert ("apps/fake/site_bad.py", "Gadget.model_validate") in flagged


def test_gate_resolves_transitive_subclasses() -> None:
    """Gadget is two levels below _AciModel (Gadget -> Widget -> _AciModel);
    the fixpoint class-hierarchy walk must still catch it."""

    flagged_symbols = {v.symbol for v in _fixture_violations()}
    assert "Gadget.model_validate" in flagged_symbols


def test_gate_passes_a_call_site_threading_reader_context() -> None:
    flagged_paths = {v.path for v in _fixture_violations()}
    assert "apps/fake/site_ok.py" not in flagged_paths


def test_gate_passes_an_allowlisted_call_site() -> None:
    flagged_paths = {v.path for v in _fixture_violations()}
    assert "apps/fake/site_allowlisted.py" not in flagged_paths


def test_gate_exempts_test_files() -> None:
    flagged_paths = {v.path for v in _fixture_violations()}
    assert "apps/fake/tests/test_something.py" not in flagged_paths


def test_gate_flags_exactly_the_one_unsanctioned_site() -> None:
    """Pins the fixture's total violation count so a change that widens or
    narrows matching is caught here, not just by drift in the other tests."""

    assert len(_fixture_violations()) == 1


def test_real_repo_call_sites_all_pass() -> None:
    """The positive-path baseline (#625): every real, current call site in this
    repo must be either a tolerant decode (threads READER_CONTEXT) or a
    declared allowlist exception. This is the actual CI gate, run against the
    real repo root and its committed allowlist.json."""

    violations = find_violations(_REPO_ROOT)
    assert violations == []
