"""Primer fetch/prefix tests. ``subprocess.run`` is monkeypatched to a fake; no
real ``agentos`` binary is invoked."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from typing import Any

import harness_eval.primer as primer
import pytest
from harness_eval.primer import PrimerUnavailable, fetch_primer, primer_prompt_prefix

_GUIDE_TEXT = "# AgentOS primer\nRun `agentos init` first. Use allowed-tools, not tools.\n"


def _fake_run_factory(returncode: int, stdout: str):  # type: ignore[no-untyped-def]
    def _fake_run(
        cmd: Sequence[str], *args: Any, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=list(cmd), returncode=returncode, stdout=stdout, stderr=""
        )

    return _fake_run


def test_fetch_primer_returns_stdout_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(primer.subprocess, "run", _fake_run_factory(0, _GUIDE_TEXT))
    assert fetch_primer("agentos") == _GUIDE_TEXT


def test_fetch_primer_raises_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(primer.subprocess, "run", _fake_run_factory(1, ""))
    with pytest.raises(PrimerUnavailable):
        fetch_primer("agentos")


def test_fetch_primer_raises_on_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("agentos not found")

    monkeypatch.setattr(primer.subprocess, "run", _boom)
    with pytest.raises(PrimerUnavailable):
        fetch_primer("agentos")


def test_fetch_primer_raises_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="agentos guide", timeout=60)

    monkeypatch.setattr(primer.subprocess, "run", _boom)
    with pytest.raises(PrimerUnavailable):
        fetch_primer("agentos")


def test_primer_prompt_prefix_wraps_primer_text() -> None:
    prefix = primer_prompt_prefix(_GUIDE_TEXT)
    # Behavioral, not format-brittle: the primer body is embedded verbatim and
    # some framing surrounds it (the returned text is longer than the input).
    assert _GUIDE_TEXT in prefix
    assert len(prefix) > len(_GUIDE_TEXT)
