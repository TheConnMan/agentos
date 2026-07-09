"""Unit tests for git-flow signature, ref mapping, and clone-url guarding."""

import hashlib
import hmac
import subprocess
from unittest import mock

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
_VALID_SHA1 = "a" * 40
_VALID_SHA256 = "a" * 64
_ALLOWED_URL = "file:///tmp/nonexistent-gitflow-repo"


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


def test_environment_for_ref_requires_exact_head_ref() -> None:
    # A tag or a nested branch that merely ends in dev/main must not deploy.
    settings = Settings()
    assert environment_for_ref("refs/tags/main", settings) is None
    assert environment_for_ref("refs/heads/feature/dev", settings) is None
    assert environment_for_ref("refs/heads/topic/main", settings) is None


def test_clone_and_archive_refuses_disallowed_scheme() -> None:
    # ext:: is git's arbitrary-command transport; it must be refused before any
    # subprocess runs, regardless of the allowlist.
    settings = Settings()
    with pytest.raises(GitFlowError):
        clone_and_archive("ext::sh -c whoami", "0" * 40, settings)


@pytest.mark.parametrize(
    "bad_sha",
    [
        "--foo",  # git-option injection via a leading dash
        "--upload-pack=touch /tmp/pwned",  # a real option-injection payload
        "-o",  # short option flag
        "deadbeef",  # too short (8 hex chars, not 40/64)
        "z" * 40,  # right length, non-hex chars
        "A" * 40,  # uppercase hex is rejected (regex is lowercase only)
        "a" * 39,  # one short of SHA-1
        "a" * 41,  # one over SHA-1 (and not SHA-256)
        "a" * 63,  # one short of SHA-256
        "a" * 65,  # one over SHA-256
        "a" * 40 + "\n",  # valid hex with a trailing newline ($ regex leak)
        "a" * 40 + "\r",  # valid hex with a trailing carriage return
        "a" * 40 + "\n--foo",  # embedded newline smuggling a git option
        "",  # empty
    ],
)
def test_clone_and_archive_rejects_invalid_sha_before_any_subprocess(
    bad_sha: str,
) -> None:
    # An invalid ref must be refused by the format gate BEFORE git ever runs, so
    # a leading-dash sha can never reach `git archive` as an injected option.
    settings = Settings()
    with mock.patch("agentos_api.gitflow.subprocess.run") as run:
        with pytest.raises(GitFlowError):
            clone_and_archive(_ALLOWED_URL, bad_sha, settings)
    run.assert_not_called()


def _completed(stdout: bytes = b"") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout)


@pytest.mark.parametrize("good_sha", [_VALID_SHA1, _VALID_SHA256])
def test_clone_and_archive_accepts_valid_hex_and_inserts_dash_dash(
    good_sha: str,
) -> None:
    # A full lowercase-hex SHA-1 or SHA-256 passes the format gate, and the
    # `git archive` argv must place a `--` separator immediately before the sha
    # so a value can never be parsed as a git option.
    settings = Settings()
    with mock.patch("agentos_api.gitflow.subprocess.run") as run:
        run.return_value = _completed(b"tar-bytes")
        result = clone_and_archive(_ALLOWED_URL, good_sha, settings)

    assert result == b"tar-bytes"

    # Locate the `git archive` invocation among the subprocess calls.
    archive_argv = next(
        call.args[0]
        for call in run.call_args_list
        if "archive" in call.args[0]
    )
    assert "--" in archive_argv, archive_argv
    dash_index = archive_argv.index("--")
    assert archive_argv[dash_index + 1] == good_sha, archive_argv
