"""Unit tests for git-flow signature, ref mapping, and clone-url guarding."""

import hashlib
import hmac

import pytest
from agentos_api.config import Settings
from agentos_api.gitflow import (
    GitFlowError,
    clone_and_archive,
    environment_for_ref,
    verify_signature,
)
from agentos_api.models import Environment

SECRET = "top-secret"


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def test_verify_signature_accepts_a_correct_digest() -> None:
    body = b'{"ref":"refs/heads/dev"}'
    assert verify_signature(SECRET, body, _sign(body)) is True


def test_verify_signature_rejects_tampered_body() -> None:
    good = _sign(b"original")
    assert verify_signature(SECRET, b"tampered", good) is False


def test_verify_signature_rejects_missing_or_malformed_header() -> None:
    assert verify_signature(SECRET, b"x", None) is False
    assert verify_signature(SECRET, b"x", "not-a-sig") is False


def test_environment_for_ref_maps_dev_and_prod_branches() -> None:
    settings = Settings()
    assert environment_for_ref("refs/heads/dev", settings) is Environment.dev
    assert environment_for_ref("refs/heads/main", settings) is Environment.prod
    assert environment_for_ref("refs/heads/feature-x", settings) is None
    assert environment_for_ref(None, settings) is None


def test_clone_and_archive_refuses_disallowed_scheme() -> None:
    # ext:: is git's arbitrary-command transport; it must be refused before any
    # subprocess runs, regardless of the allowlist.
    settings = Settings()
    with pytest.raises(GitFlowError):
        clone_and_archive("ext::sh -c whoami", "0" * 40, settings)
