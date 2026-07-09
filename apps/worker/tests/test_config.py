"""Regression tests for WorkerConfig env-source resolution.

``populate_by_name=True`` lets tests construct the config with field-name
kwargs, but it must NOT make the env source read the bare uppercased field name
as a fallback for a field that carries a ``validation_alias``. An aliased field
must read only its ``AGENTOS_*`` alias; a stray generic env var (``API_KEY``,
``CREDENTIALS``, ...) in the pod env must be ignored, as it was before the
BaseSettings refactor.
"""

from __future__ import annotations

import pytest
from agentos_worker.config import WorkerConfig


def test_aliased_field_ignores_bare_field_name_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stray bare-name env var must not leak into an aliased field."""
    monkeypatch.setenv("API_KEY", "stray")
    monkeypatch.setenv("CREDENTIALS", "stray-creds")

    config = WorkerConfig()

    assert config.api_key == "agentos-dev-key"  # the default, not "stray"
    assert config.credentials == ""  # the default, not "stray-creds"


def test_aliased_field_reads_its_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """The intended AGENTOS_* alias is still read from the env."""
    monkeypatch.setenv("AGENTOS_API_KEY", "intended")
    monkeypatch.setenv("AGENTOS_CREDENTIALS", "intended-creds")

    config = WorkerConfig()

    assert config.api_key == "intended"
    assert config.credentials == "intended-creds"


def test_alias_wins_over_bare_field_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """With both set, only the alias is read and the bare name is ignored."""
    monkeypatch.setenv("API_KEY", "stray")
    monkeypatch.setenv("AGENTOS_API_KEY", "intended")

    assert WorkerConfig().api_key == "intended"


def test_field_name_kwargs_still_populate() -> None:
    """populate_by_name construction (used by tests) is unchanged."""
    config = WorkerConfig(fake_model=True, api_key="x", credentials="c")

    assert config.fake_model is True
    assert config.api_key == "x"
    assert config.credentials == "c"


def test_non_aliased_field_still_reads_plain_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fields without an alias keep reading their uppercased field name."""
    monkeypatch.setenv("VALKEY_HOST", "valkey.internal")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/agentos"
    )

    config = WorkerConfig()

    assert config.valkey_host == "valkey.internal"
    assert config.database_url == "postgresql+asyncpg://u:p@db:5432/agentos"
