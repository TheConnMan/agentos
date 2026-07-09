"""RouteRecord wire-format contract for the per-sandbox runner token (issue #63).

The token is carried on the SandboxHandle from claim-time to call-time, and the
affinity store serializes the handle to Valkey, so the token must survive the
JSON round-trip. Upgrade safety: a route written by a pre-token worker has no
``token`` key, and must rehydrate to ``token == ""`` (that sandbox's runner has
no token configured, so the client sends no header and it keeps working).
"""

from __future__ import annotations

import json

from agentos_worker.sandbox.types import RouteRecord, SandboxHandle


def _handle(**overrides: object) -> SandboxHandle:
    base: dict[str, object] = {
        "thread_key": "t",
        "claim_name": "c",
        "sandbox_name": "s",
        "namespace": "n",
        "service_fqdn": "s.n.svc.cluster.local",
        "port": 8080,
        "session_id": "sess",
    }
    base.update(overrides)
    return SandboxHandle(**base)  # type: ignore[arg-type]


def test_route_record_round_trips_token() -> None:
    record = RouteRecord(handle=_handle(token="tok-20"))
    restored = RouteRecord.from_json(record.to_json())

    assert restored.handle.token == "tok-20"
    assert restored.handle == record.handle


def test_route_record_legacy_payload_without_token_defaults_empty() -> None:
    # A route written before the token field existed: from_json must not crash and
    # must default the token to "" (upgrade compatibility, the deploy-safety case).
    legacy = {
        "thread_key": "t",
        "claim_name": "c",
        "sandbox_name": "s",
        "namespace": "n",
        "service_fqdn": "s.n.svc.cluster.local",
        "port": 8080,
        "session_id": "sess",
        "history_ref": None,
        "state": "live",
    }
    record = RouteRecord.from_json(json.dumps(legacy))
    assert record.handle.token == ""
