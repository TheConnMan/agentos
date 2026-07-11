"""Bind shared credential classes to the OpenCode harness environment.

Credential classification belongs to the harness neutral runner seam established
by ADR 0009 and PR 306. This module is the per harness binder: it consumes the
shared ``classify_credential`` result and selects only the environment variable
that OpenCode supports. Unsupported classes fail before a model session starts so
credentials are never silently dropped.
"""

from __future__ import annotations

from collections.abc import Mapping

from ..sdk_auth import (
    CREDENTIALS_ENV,
    UnsupportedCredentialError,
    classify_credential,
)

OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"


def resolve_opencode_env(env: Mapping[str, str]) -> dict[str, str]:
    """Resolve ``AGENTOS_CREDENTIALS`` into the OpenCode process environment."""
    credential = env.get(CREDENTIALS_ENV)
    if not credential:
        return {}

    credential_class = classify_credential(credential)
    if credential_class == "openrouter":
        return {OPENROUTER_API_KEY_ENV: credential}

    raise UnsupportedCredentialError(
        f"AGENTOS_CREDENTIALS has unsupported class {credential_class} for OpenCode"
    )
