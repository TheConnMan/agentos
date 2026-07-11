"""The OpenCode stub bundle installer: an explicit, documented no-op (issue #309)."""

import logging

from agentos_runner.opencode import OpenCodeBundleInstaller
from agentos_runner.plugin import BundleInstaller

_INSTALLER_LOGGER = "agentos_runner.opencode.installer"


def test_no_dir_returns_none_without_warning(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=_INSTALLER_LOGGER):
        assert OpenCodeBundleInstaller().install(None) is None
    assert caplog.records == []


def test_configured_bundle_returns_none_and_warns(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=_INSTALLER_LOGGER):
        assert OpenCodeBundleInstaller().install("/some/bundle") is None
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1
    warning = messages[0]
    assert "/some/bundle" in warning
    assert "bundle-less" in warning
    assert "#310" in warning


def test_opencode_installer_satisfies_bundle_installer_port() -> None:
    assert isinstance(OpenCodeBundleInstaller(), BundleInstaller)
