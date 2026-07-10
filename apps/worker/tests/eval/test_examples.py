"""Every committed example bundle must load under the frozen eval-case schema.

This is the durable gate against a shipped example drifting back to a retired
shape: it globs every ``examples/**/evals/cases.json`` and parses each as an
``EvalSuite``. If a first-party example is not platform-loadable, the README
onboarding command ``agentos skill eval`` would hard-fail on it, so this test
fails first.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agentos_worker.eval.models import EvalSuite

# apps/worker/tests/eval/test_examples.py -> parents[4] is the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_EXAMPLE_CASES = sorted(_REPO_ROOT.glob("examples/**/evals/cases.json"))


def test_at_least_one_example_bundle_is_shipped() -> None:
    """A vacuous glob must not let this gate pass silently."""
    assert _EXAMPLE_CASES, f"no example cases.json found under {_REPO_ROOT}/examples"


@pytest.mark.parametrize("path", _EXAMPLE_CASES, ids=lambda p: str(p))
def test_example_bundle_loads_as_eval_suite(path: Path) -> None:
    """Each shipped example parses under the frozen eval-case schema."""
    EvalSuite.model_validate_json(path.read_text(encoding="utf-8"))
