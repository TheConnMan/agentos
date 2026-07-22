"""Reserved sandbox boot-env name policy for connector secrets (#457, #445, ADR-0009).

Single source of truth for the names a per-agent connector secret must never
declare, consulted by every write seam so a connector secret cannot shadow a
runner-owned model credential or a platform boot-env key, NOR a generic
redirect/capture-capable key the SDK's HTTP/TLS stack reads (#487):

- API validator ``agentos_api.schemas._validate_secret_map``,
- bundle validator ``plugin_format.validate._validate_secrets``,
- worker injection loop ``agentos_worker.binding``,
- Helm guard ``charts/agentos/templates/agent-connector-secrets.yaml``.

#445 fenced only the ``AGENTOS_`` prefix, which structurally cannot see the four
non-prefixed credential keys the runner's ``sdk_auth`` owns
(``ANTHROPIC_BASE_URL``, ``ANTHROPIC_API_KEY``, ``CLAUDE_CODE_OAUTH_TOKEN``,
``ANTHROPIC_AUTH_TOKEN``). A connector secret named ``ANTHROPIC_BASE_URL`` would
silently redirect the Claude session to an arbitrary endpoint and hand it the
resolved model credential. #457 closes that gap by naming those keys explicitly
while keeping the whole ``AGENTOS_`` namespace fenced for forward safety.

The real owners of these names are ``runner/src/agentos_runner/sdk_auth.py`` (the
credential keys) and ``apps/worker/src/agentos_worker/binding.py`` (the
``AGENTOS_*`` boot keys). This module re-enumerates them so the policy is
greppable from one place; ``apps/worker/tests/binding/test_reserved_boot_env_pin.py``
is the completeness + cross-language drift pin that fails CI if a boot or
credential key is added there but not covered here (or if the Helm list drifts).
"""

from __future__ import annotations

# The four runner ``sdk_auth`` credential keys that are NOT ``AGENTOS_``-prefixed.
# These are the exact gap #457 closes. Together with ``_REDIRECT_CAPTURE_KEYS``
# below, these are the non-prefixed members of the set; per the drift pin, the
# Helm ``_helpers.tpl`` reserved list must equal that combined non-prefixed subset.
_CREDENTIAL_KEYS = frozenset(
    {
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_AUTH_TOKEN",
    }
)

# Generic, non-``AGENTOS_``-prefixed env that the SDK's HTTP/TLS stack (or Node)
# reads to REDIRECT or CAPTURE the model session, rather than being a key the
# worker/runner explicitly owns (#487). Each reaches the same end state #457 closed
# for ``ANTHROPIC_BASE_URL``: a connector secret named one of these could route the
# session (and its resolved credential) through an operator-named proxy, add a
# trusted CA for transparent TLS MITM, or inject arbitrary headers onto model
# calls. Fenced here so the reserved set means "reserved OR redirect/capture-
# capable", not merely "keys we own".
_REDIRECT_CAPTURE_KEYS = frozenset(
    {
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NODE_EXTRA_CA_CERTS",
        "ANTHROPIC_CUSTOM_HEADERS",
    }
)

# The ``AGENTOS_``-prefixed boot/config keys the worker and runner own. Every one
# is already caught by the prefix rule in ``is_reserved_boot_env_name``; they are
# enumerated here for greppability and to make the completeness pin
# membership-based. Cross-checked against ``aci_protocol.BootEnv.env_keys()`` (the
# declared boot contract since #488) and ``sdk_auth.MODEL_BASE_URL_ENV``.
#
# This list is enumeration, not enforcement: the prefix rule is what actually
# reserves an ``AGENTOS_`` name, so adding or dropping an entry here is
# policy-neutral (pinned by test). ``AGENTOS_AGENT_ID`` was dropped in #488 when
# its write site went away; it stays reserved via the prefix.
_AGENTOS_BOOT_KEYS = frozenset(
    {
        "AGENTOS_MODEL_BASE_URL",
        "AGENTOS_MODEL_API_BACKEND",
        "AGENTOS_MODEL_ENV_KEY",
        "AGENTOS_CREDENTIALS",
        "AGENTOS_MODEL",
        "AGENTOS_FAKE_MODEL",
        "AGENTOS_BUDGET",
        "AGENTOS_SESSION_ID",
        "AGENTOS_BUNDLE_REF",
        "AGENTOS_PLUGIN_DIR",
        "AGENTOS_MEMORY_REF",
        "AGENTOS_MEMORY_TOKEN",
        "AGENTOS_STATE_URL",
        "AGENTOS_STATE_TOKEN",
        "AGENTOS_HISTORY_REF",
        "AGENTOS_HISTORY_TOKEN",
        "AGENTOS_RUNNER_TOKEN",
        "AGENTOS_APPROVAL_REQUIRED_TOOLS",
        "AGENTOS_CONNECTOR_SECRET_KEYS",
        "AGENTOS_APPROVAL_GRANT_TOOL",
        "AGENTOS_SANDBOX_ID",
        "AGENTOS_RUNNER_PORT",
    }
)

RESERVED_BOOT_ENV: frozenset[str] = (
    _CREDENTIAL_KEYS | _REDIRECT_CAPTURE_KEYS | _AGENTOS_BOOT_KEYS
)


def is_reserved_boot_env_name(name: str) -> bool:
    """Whether ``name`` is a reserved sandbox boot-env / model-credential key.

    ``True`` for any explicitly enumerated key in ``RESERVED_BOOT_ENV`` and for
    the whole ``AGENTOS_`` namespace (the forward-safe catch-all covering future
    boot keys nobody remembers to enumerate). Connector secrets must not declare
    such a name.
    """
    return name in RESERVED_BOOT_ENV or name.startswith("AGENTOS_")
