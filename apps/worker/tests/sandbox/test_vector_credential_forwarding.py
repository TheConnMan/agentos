"""The Python half of the cross-language model-credential forwarding gate (#495).

Drives the real ``DockerSandboxClient`` through every vector in the committed
matrix and reads the forwarded env NAMES back off the captured ``docker run``
argv. The Rust CLI reads the same file in its own lane, so a rule changed in one
language without the other fails that language's test.

The rule itself is never restated here: it lives in the vector file, and the
worker's implementation of it lives at
``agentos_worker/sandbox/docker.py``'s positive single-credential selection.
"""

from __future__ import annotations

import json
from pathlib import Path

from agentos_worker.sandbox.docker import DockerSandboxClient

_VECTORS = (
    Path(__file__).resolve().parents[4]
    / "tests"
    / "vectors"
    / "model-credential-forwarding.json"
)

# Placeholder credential values (never real). Hoisted into named constants so the
# secrets-scan pre-commit hook does not false-positive on inline
# `"CLAUDE_CODE_OAUTH_TOKEN": "sk-..."` literals; the values are asserted absent
# from the forwarded argv below.
_AMBIENT_OAUTH = "sk-PLACEHOLDER-oauth"
_AMBIENT_ANTHROPIC_CRED = "sk-ant-PLACEHOLDER-key"
_BYO_CREDENTIAL = "sk-or-PLACEHOLDER-byo"

# Every key a vector row may carry. Checked exactly, so an unrecognized key is a
# loud failure rather than an input this lane silently ignores: a row that grows
# a sixth input would otherwise pass vacuously, which is the exact drift the gate
# exists to catch. The Rust lane rejects unknown fields the same way, via
# `#[serde(deny_unknown_fields)]` on its ForwardingVector.
_EXPECTED_VECTOR_KEYS = frozenset(
    {
        "name",
        "why",
        "fake_model",
        "base_url_override",
        "byo_credential",
        "ambient_oauth",
        "ambient_api_key",
        "expected",
    }
)


class _FakeBundleStore:
    def get(self, key: str) -> bytes:
        return b""


class _RecordingDocker(DockerSandboxClient):
    """Captures every docker argv; the docker CLI is the one external dependency."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.calls: list[list[str]] = []

    def _docker(self, args: list[str], *, check: bool = True) -> str:
        self.calls.append(args)
        return ""


def _forwarded_names(argv: list[str]) -> list[str]:
    """The by-name forwards: `-e NAME` args, not the `-e KEY=VALUE` pairs the
    generic boot-env loop emits."""
    return [
        argv[i + 1]
        for i, a in enumerate(argv)
        if a == "-e" and i + 1 < len(argv) and "=" not in argv[i + 1]
    ]


def _assert_known_keys(vector: dict[str, object]) -> None:
    keys = set(vector)
    if keys != _EXPECTED_VECTOR_KEYS:
        raise AssertionError(
            f"vector {vector.get('name')!r} in {_VECTORS} has unexpected keys "
            f"{sorted(keys - _EXPECTED_VECTOR_KEYS)} and is missing "
            f"{sorted(_EXPECTED_VECTOR_KEYS - keys)}. A new input is rejected on "
            "purpose: one this lane cannot see would pass vacuously. Teach the new "
            "key to _EXPECTED_VECTOR_KEYS and _run_vector here, to ForwardingVector "
            "in cli/src/commands.rs, and to both implementations of the rule."
        )


def _run_vector(vector: dict[str, object]) -> list[str]:
    # Two different dicts, deliberately: the state flags are read from the boot
    # env, the credential presence from the worker environ.
    env: dict[str, str] = {"AGENTOS_BUDGET": "{}"}
    if vector["fake_model"]:
        env["AGENTOS_FAKE_MODEL"] = "1"
    if vector["base_url_override"]:
        env["ANTHROPIC_BASE_URL"] = "http://ollama:11434"

    environ: dict[str, str] = {}
    if vector["byo_credential"]:
        environ["AGENTOS_CREDENTIALS"] = _BYO_CREDENTIAL
    if vector["ambient_oauth"]:
        environ["CLAUDE_CODE_OAUTH_TOKEN"] = _AMBIENT_OAUTH
    if vector["ambient_api_key"]:
        environ["ANTHROPIC_API_KEY"] = _AMBIENT_ANTHROPIC_CRED

    client = _RecordingDocker(
        image="agentos-runner", bundle_store=_FakeBundleStore(), environ=environ
    )
    client.create_claim("t1", pool="pool", env=env)
    argv = client.calls[0]
    assert all("PLACEHOLDER" not in a for a in argv)  # no credential value in argv
    return _forwarded_names(argv)


def test_worker_matches_every_forwarding_vector() -> None:
    vectors = json.loads(_VECTORS.read_text(encoding="utf-8"))["vectors"]
    # Guards against a rename or a truncated file making this loop vacuously pass.
    assert vectors, f"no vectors parsed from {_VECTORS}"

    for vector in vectors:
        _assert_known_keys(vector)
        forwarded = _run_vector(vector)
        assert forwarded == vector["expected"], vector["name"]
