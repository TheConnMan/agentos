"""Lock the fake-driver CLI smoke contract.

Drives the ``__main__`` entry point through the fake driver and JSON format,
capturing stdout, and asserts the rebuilt ``DeltaReport`` shows a positive
primer lift. Deterministic: no subprocess, no network, no token spend.
"""

from __future__ import annotations

import harness_eval.__main__ as main_mod
import pytest
from harness_eval.models import DeltaReport


def test_main_fake_json_reports_positive_lift(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main_mod.main(["run", "--driver", "fake", "--format", "json"])
    assert exit_code == 0

    captured = capsys.readouterr()
    report = DeltaReport.model_validate_json(captured.out)

    assert report.accuracy_delta > 0


def test_main_fake_summary_prints_headline(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main_mod.main(["run", "--driver", "fake", "--format", "summary"])
    assert exit_code == 0

    out = capsys.readouterr().out
    assert "accuracy" in out
    assert "50.0%" in out
    assert "100.0%" in out
