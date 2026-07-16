"""Completeness + cross-language drift pin for the reserved boot-env policy (#457).

Three guards, all checked at import time (no Postgres, no fixtures):

(a) Every credential-key literal the runner's ``sdk_auth`` owns is caught by
    ``is_reserved_boot_env_name``. If a new model credential is added to
    ``sdk_auth`` but not to ``RESERVED_BOOT_ENV``, this fails -- the exact
    class of gap #457 closes.  ``AGENTOS_MODEL_BASE_URL`` / ``AGENTOS_CREDENTIALS``
    are already safe via the prefix rule, but the pin asserts them anyway so the
    sdk_auth inventory is covered exhaustively.
(b) Every ``agentos_worker.binding`` ``*_ENV`` literal that starts with
    ``AGENTOS_`` is caught. This passes today via the prefix rule; it is a
    tripwire against the predicate silently regressing.
(c) Cross-language parity: the Helm ``_helpers.tpl`` reserved list is an
    unavoidable second copy (Helm cannot import Python). Its
    ``agentos.reservedConnectorSecretNames`` define MUST list exactly the
    non-``AGENTOS_`` members of ``RESERVED_BOOT_ENV`` (the prefix rule covers
    ``AGENTOS_*`` on both sides). Fails CI if the two lists drift.
"""

from __future__ import annotations

import re
from pathlib import Path

import agentos_runner.sdk_auth as sdk_auth
import agentos_worker.binding as binding
from plugin_format import RESERVED_BOOT_ENV, is_reserved_boot_env_name

# --- (a) sdk_auth credential-key literals ------------------------------------

# An env-var-name string: uppercase alnum, underscore-separated (ANTHROPIC_BASE_URL,
# AGENTOS_MODEL_BASE_URL, ...). Discriminates a credential-key literal from a base
# URL like "https://openrouter.ai/api" or a tuple constant.
_ENV_VAR_NAME_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")


def _sdk_auth_credential_env_literals() -> dict[str, str]:
    """Discover the credential/base-url env-var literals sdk_auth owns.

    Every module-level ``*_ENV`` name in ``agentos_runner.sdk_auth`` whose value
    is an env-var-name string. Reading the module: all of them
    (``CLAUDE_CODE_OAUTH_TOKEN``, ``ANTHROPIC_API_KEY``, ``ANTHROPIC_BASE_URL``,
    ``ANTHROPIC_AUTH_TOKEN``, ``AGENTOS_MODEL_BASE_URL``, ``AGENTOS_CREDENTIALS``)
    are credential/base-url keys that MUST be reserved -- there is no runner-local
    ``*_ENV`` knob here to exclude. The tuple alias ``_SDK_CREDENTIAL_ENV`` is
    skipped by the string check. Dynamic (not a hardcoded list) so a NEW credential
    ``*_ENV`` constant added to sdk_auth is caught even if nobody updates the pin.
    """
    out: dict[str, str] = {}
    for attr in dir(sdk_auth):
        if not attr.endswith("_ENV"):
            continue
        value = getattr(sdk_auth, attr)
        if isinstance(value, str) and _ENV_VAR_NAME_RE.match(value):
            out[attr] = value
    return out


def test_every_sdk_auth_credential_key_is_reserved() -> None:
    literals = _sdk_auth_credential_env_literals()
    # Sanity floor: discovery is not vacuous, and the four non-AGENTOS_ credential
    # keys plus the AGENTOS_ base-url alias are all present (guards the predicate
    # silently narrowing and skipping the exact gap #457 closes).
    assert {
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_AUTH_TOKEN",
        "AGENTOS_MODEL_BASE_URL",
    } <= set(literals.values()), literals
    for attr, value in literals.items():
        assert is_reserved_boot_env_name(value), (
            f"sdk_auth.{attr} == {value!r} is not caught by the reserved policy"
        )


# --- (b) binding AGENTOS_* *_ENV literals ------------------------------------


def _binding_agentos_env_literals() -> dict[str, str]:
    out: dict[str, str] = {}
    for attr in dir(binding):
        if not attr.endswith("_ENV"):
            continue
        value = getattr(binding, attr)
        if isinstance(value, str) and value.startswith("AGENTOS_"):
            out[attr] = value
    return out


def test_every_binding_agentos_env_literal_is_reserved() -> None:
    literals = _binding_agentos_env_literals()
    # Sanity: the introspection actually found the boot keys (guards against a
    # rename making this test vacuously pass).
    assert literals, "found no AGENTOS_* *_ENV literals in agentos_worker.binding"
    for attr, value in literals.items():
        assert is_reserved_boot_env_name(value), (
            f"binding.{attr} == {value!r} is not caught by the reserved policy"
        )


# --- (c) Helm cross-language drift gate --------------------------------------

_HELPERS_TPL = (
    Path(__file__).resolve().parents[4]
    / "charts"
    / "agentos"
    / "templates"
    / "_helpers.tpl"
)

# An env-name token: uppercase, at least one underscore (ANTHROPIC_BASE_URL etc).
_ENV_NAME_RE = re.compile(r"[A-Z0-9]+(?:_[A-Z0-9]+)+")


def _reserved_names_from_helpers() -> set[str]:
    text = _HELPERS_TPL.read_text(encoding="utf-8")
    # Extract the body of the reservedConnectorSecretNames define, tolerantly.
    match = re.search(
        r'define\s+"agentos\.reservedConnectorSecretNames"\s*(?:-?}})?(?P<body>.*?){{-?\s*end',
        text,
        re.DOTALL,
    )
    assert match, (
        "no `agentos.reservedConnectorSecretNames` define found in "
        f"{_HELPERS_TPL} -- the Helm reserved-name drift gate has no source"
    )
    tokens = set(_ENV_NAME_RE.findall(match.group("body")))
    # The prefix rule covers AGENTOS_* on both sides; only the explicitly
    # enumerated credential keys need list-parity.
    return {t for t in tokens if not t.startswith("AGENTOS_")}


def test_helm_reserved_list_matches_non_prefixed_members() -> None:
    expected = {n for n in RESERVED_BOOT_ENV if not n.startswith("AGENTOS_")}
    assert _reserved_names_from_helpers() == expected
