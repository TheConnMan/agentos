"""Pure-unit pin: the worker binding's and the k8s substrate's
``CONNECTOR_SECRET_KEYS_ENV`` literals must agree (#465, #429).

The worker binding and the Kubernetes substrate each define
``CONNECTOR_SECRET_KEYS_ENV`` locally -- the substrate deliberately avoids a
binding import to keep the seam clean (see the comment at
``sandbox/k8s.py:52-58``). If the two literals ever diverge, the substrate
silently stops recognizing the marker it uses to strip connector-secret
values off the value-only SandboxClaim CR, and every per-agent connector
secret is persisted as plaintext in etcd instead of being stripped.

This runs with NO Postgres and NO fixtures: both constants are imported at
module import time, so the pin is checked in EVERY test lane.
"""

from __future__ import annotations

from agentos_worker.binding import CONNECTOR_SECRET_KEYS_ENV as BINDING_KEY
from agentos_worker.sandbox.k8s import CONNECTOR_SECRET_KEYS_ENV as K8S_KEY


def test_connector_secret_keys_env_literals_agree() -> None:
    assert BINDING_KEY == K8S_KEY, (
        "CONNECTOR_SECRET_KEYS_ENV diverged between agentos_worker.binding and "
        "agentos_worker.sandbox.k8s: the substrate would silently stop stripping "
        "connector secrets, persisting them as plaintext in etcd"
    )
