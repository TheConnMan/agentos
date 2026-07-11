"""Shared fixtures for the harness-eval test suite.

Deliberately imports nothing from ``harness_eval``: the package source does not
exist yet, so keeping this module import-clean lets each test file fail on its
own missing-import line instead of aborting collection for the whole suite.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

WorkspaceFactory = Callable[[Mapping[str, str] | None], Path]


@pytest.fixture
def workspace_factory(tmp_path: Path) -> WorkspaceFactory:
    """Return a builder that materializes a fresh workspace dir from a
    ``{relpath: content}`` mapping and hands back its path.

    Each call gets a distinct directory under ``tmp_path`` so a single test can
    build several independent workspaces (e.g. a PASS and a FAIL case)."""

    counter = itertools.count()

    def make(files: Mapping[str, str] | None = None) -> Path:
        ws = tmp_path / f"ws-{next(counter)}"
        ws.mkdir(parents=True, exist_ok=True)
        for rel, content in (files or {}).items():
            target = ws / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return ws

    return make
