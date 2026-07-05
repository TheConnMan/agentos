"""Kubernetes access for the sandbox substrate.

``SandboxClient`` is the seam the substrate is written against; the
``KubernetesSandboxClient`` implementation drives the agent-sandbox v0.5.0 CRDs
(core group ``agents.x-k8s.io`` for ``Sandbox``, extensions group
``extensions.agents.x-k8s.io`` for ``SandboxClaim``) via the official client's
CustomObjectsApi. Unit tests use an in-memory fake of the protocol (the K8s
control plane is an external service); the real implementation is exercised by
the k8scratch e2e test.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config

from .types import ClaimView, SandboxView

CORE_GROUP = "agents.x-k8s.io"
CORE_VERSION = "v1beta1"
EXT_GROUP = "extensions.agents.x-k8s.io"
EXT_VERSION = "v1beta1"

MANAGED_BY_LABEL = "agentos.dev/managed-by"
MANAGED_BY_VALUE = "agentos-sandbox-substrate"
THREAD_HASH_LABEL = "agentos.dev/thread-hash"

OperatingMode = Literal["Running", "Suspended"]


class SandboxClient(Protocol):
    """What the substrate needs from the cluster, and nothing more."""

    def create_claim(
        self,
        name: str,
        *,
        pool: str,
        env: dict[str, str] | None = None,
        labels: dict[str, str] | None = None,
    ) -> None: ...

    def get_claim(self, name: str) -> ClaimView | None: ...

    def delete_claim(self, name: str) -> None: ...

    def list_claims(self, *, label_selector: str) -> list[ClaimView]: ...

    def get_sandbox(self, name: str) -> SandboxView | None: ...

    def set_sandbox_mode(self, name: str, mode: OperatingMode) -> None: ...


def _conditions_ready(status: dict[str, Any]) -> bool:
    for cond in status.get("conditions") or []:
        if cond.get("type") == "Ready":
            return bool(cond.get("status") == "True")
    return False


def _claim_view(obj: dict[str, Any]) -> ClaimView:
    status = obj.get("status") or {}
    sandbox = (status.get("sandbox") or {}).get("name")
    return ClaimView(
        name=obj["metadata"]["name"],
        ready=_conditions_ready(status),
        sandbox_name=sandbox,
        labels=dict(obj["metadata"].get("labels") or {}),
    )


def _sandbox_view(obj: dict[str, Any]) -> SandboxView:
    status = obj.get("status") or {}
    return SandboxView(
        name=obj["metadata"]["name"],
        ready=_conditions_ready(status),
        service_fqdn=status.get("serviceFQDN") or None,
        operating_mode=str((obj.get("spec") or {}).get("operatingMode", "Running")),
    )


class KubernetesSandboxClient:
    """SandboxClient against a real cluster (kubeconfig or in-cluster auth)."""

    def __init__(self, namespace: str, *, kubeconfig: str | None = None) -> None:
        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            k8s_config.load_kube_config(config_file=kubeconfig)
        self._api = k8s_client.CustomObjectsApi()
        self._namespace = namespace

    # -- SandboxClaim (extensions group) ------------------------------------

    def create_claim(
        self,
        name: str,
        *,
        pool: str,
        env: dict[str, str] | None = None,
        labels: dict[str, str] | None = None,
    ) -> None:
        body: dict[str, Any] = {
            "apiVersion": f"{EXT_GROUP}/{EXT_VERSION}",
            "kind": "SandboxClaim",
            "metadata": {
                "name": name,
                "labels": {MANAGED_BY_LABEL: MANAGED_BY_VALUE, **(labels or {})},
            },
            "spec": {"warmPoolRef": {"name": pool}},
        }
        if env:
            body["spec"]["env"] = [{"name": k, "value": v} for k, v in sorted(env.items())]
        self._api.create_namespaced_custom_object(
            EXT_GROUP, EXT_VERSION, self._namespace, "sandboxclaims", body
        )

    def get_claim(self, name: str) -> ClaimView | None:
        obj = self._get(EXT_GROUP, EXT_VERSION, "sandboxclaims", name)
        return _claim_view(obj) if obj is not None else None

    def delete_claim(self, name: str) -> None:
        try:
            self._api.delete_namespaced_custom_object(
                EXT_GROUP, EXT_VERSION, self._namespace, "sandboxclaims", name
            )
        except k8s_client.ApiException as exc:
            if exc.status != 404:
                raise

    def list_claims(self, *, label_selector: str) -> list[ClaimView]:
        result = self._api.list_namespaced_custom_object(
            EXT_GROUP,
            EXT_VERSION,
            self._namespace,
            "sandboxclaims",
            label_selector=label_selector,
        )
        return [_claim_view(item) for item in result.get("items", [])]

    # -- Sandbox (core group) ------------------------------------------------

    def get_sandbox(self, name: str) -> SandboxView | None:
        obj = self._get(CORE_GROUP, CORE_VERSION, "sandboxes", name)
        return _sandbox_view(obj) if obj is not None else None

    def set_sandbox_mode(self, name: str, mode: OperatingMode) -> None:
        self._api.patch_namespaced_custom_object(
            CORE_GROUP,
            CORE_VERSION,
            self._namespace,
            "sandboxes",
            name,
            {"spec": {"operatingMode": mode}},
        )

    # -- helpers --------------------------------------------------------------

    def _get(self, group: str, version: str, plural: str, name: str) -> dict[str, Any] | None:
        try:
            obj = self._api.get_namespaced_custom_object(
                group, version, self._namespace, plural, name
            )
        except k8s_client.ApiException as exc:
            if exc.status == 404:
                return None
            raise
        return dict(obj)
