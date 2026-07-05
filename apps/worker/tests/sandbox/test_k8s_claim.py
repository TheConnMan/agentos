"""KubernetesSandboxClient.create_claim payload shape.

The agent-sandbox controller injects per-claim env with no ``containerName`` into
only the first main container. The bundle ref must ALSO be targeted at the init
containers by name, or a Kubernetes runner boots an empty plugin dir. These tests
assert the emitted SandboxClaim env, so the fix is mutation-honest: dropping the
named entries fails ``test_bundle_ref_targets_init_containers_by_name``.
"""

from __future__ import annotations

from typing import Any

from agentos_worker.sandbox.k8s import (
    BUNDLE_INIT_CONTAINERS,
    KubernetesSandboxClient,
)


class _FakeApi:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def create_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, body: dict[str, Any]
    ) -> None:
        self.created.append(body)


def _client(api: _FakeApi) -> KubernetesSandboxClient:
    client = KubernetesSandboxClient.__new__(KubernetesSandboxClient)
    client._api = api  # type: ignore[attr-defined]
    client._namespace = "test-ns"  # type: ignore[attr-defined]
    return client


def _env_entries(api: _FakeApi) -> list[dict[str, str]]:
    return api.created[0]["spec"]["env"]


def test_bundle_ref_targets_init_containers_by_name() -> None:
    api = _FakeApi()
    _client(api).create_claim(
        "claim-1",
        pool="pool",
        env={"AGENTOS_BUNDLE_REF": "bundles/x.tar.gz", "AGENTOS_BUDGET": "{}"},
    )
    entries = _env_entries(api)

    # The main runner still receives the ref (unnamed entry).
    assert {"name": "AGENTOS_BUNDLE_REF", "value": "bundles/x.tar.gz"} in entries

    # And each bundle init container receives it by explicit containerName.
    named = {
        (e["containerName"], e["name"]): e["value"] for e in entries if "containerName" in e
    }
    for container in BUNDLE_INIT_CONTAINERS:
        assert named[(container, "AGENTOS_BUNDLE_REF")] == "bundles/x.tar.gz"


def test_no_named_env_without_bundle_ref() -> None:
    api = _FakeApi()
    _client(api).create_claim(
        "claim-1", pool="pool", env={"AGENTOS_BUDGET": "{}", "AGENTOS_SESSION_ID": "s"}
    )
    entries = _env_entries(api)
    assert entries  # the main-container env is still present
    assert all("containerName" not in e for e in entries)


def test_credential_is_never_written_to_the_claim() -> None:
    # The SandboxClaim env is value-only, so the secret must not be persisted on
    # the claim; the template's secretKeyRef supplies it to the runner instead.
    api = _FakeApi()
    _client(api).create_claim(
        "claim-1",
        pool="pool",
        env={"AGENTOS_BUDGET": "{}", "AGENTOS_CREDENTIALS": "super-secret-token"},
    )
    entries = _env_entries(api)
    assert all(e.get("name") != "AGENTOS_CREDENTIALS" for e in entries)
    assert all("super-secret-token" not in e.get("value", "") for e in entries)
    # The rest of the boot env is still written.
    assert {"name": "AGENTOS_BUDGET", "value": "{}"} in entries
