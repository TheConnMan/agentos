"""Regression tests for DispatcherConfig env-source resolution.

``populate_by_name=True`` lets tests construct the config with field-name
kwargs, but it must NOT make the env source read the bare uppercased field name
as a fallback for a field that carries a ``validation_alias``. An aliased field
must read only its ``AGENTOS_*`` alias; a stray generic env var (``STREAM``,
``SHIMMER``, ...) in the pod env must be ignored, as it was before the
BaseSettings refactor.
"""

from __future__ import annotations

import pytest
from agentos_dispatcher.config import DispatcherConfig


def test_aliased_field_ignores_bare_field_name_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stray bare-name env var must not leak into an aliased field."""
    monkeypatch.setenv("STREAM", "stray:stream")

    config = DispatcherConfig()

    assert config.stream == "agentos:runs"  # the default, not "stray:stream"


def test_aliased_field_reads_its_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """The intended AGENTOS_* alias is still read from the env."""
    monkeypatch.setenv("AGENTOS_STREAM", "intended:stream")

    assert DispatcherConfig().stream == "intended:stream"


def test_alias_wins_over_bare_field_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """With both set, only the alias is read and the bare name is ignored."""
    monkeypatch.setenv("STREAM", "stray:stream")
    monkeypatch.setenv("AGENTOS_STREAM", "intended:stream")

    assert DispatcherConfig().stream == "intended:stream"


def test_field_name_kwargs_still_populate() -> None:
    """populate_by_name construction (used by tests) is unchanged."""
    config = DispatcherConfig(stream="s", shimmer=True)

    assert config.stream == "s"
    assert config.shimmer is True


def test_non_aliased_field_still_reads_plain_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fields without an alias keep reading their uppercased field name."""
    monkeypatch.setenv("VALKEY_HOST", "valkey.internal")

    assert DispatcherConfig().valkey_host == "valkey.internal"
