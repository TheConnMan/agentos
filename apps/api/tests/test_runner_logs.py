"""Runner-logs proxy: no-cluster degradation, success, and error mapping.

The K8s access is injected, so success and error paths are exercised with a fake
reader; the default app (no cluster configured) proves the 503 degradation.
"""

from typing import Any

from agentos_api.deps import get_pod_log_reader
from agentos_api.k8s import NullPodLogReader, PodLogError, build_pod_log_reader
from agentos_api.main import create_app
from fastapi.testclient import TestClient

URL = "/observability/runners/preview-pr-1/runner-abc/logs"


class FakeReader:
    def read(
        self,
        namespace: str,
        pod: str,
        container: str | None,
        tail_lines: int | None,
        previous: bool,
    ) -> str:
        return f"logs for {namespace}/{pod}"


class NotFoundReader:
    def read(
        self,
        namespace: str,
        pod: str,
        container: str | None,
        tail_lines: int | None,
        previous: bool,
    ) -> str:
        raise PodLogError("pod not found", status=404)


def _client_with(reader: object) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_pod_log_reader] = lambda: reader
    return TestClient(app)


def test_no_cluster_configured_degrades_to_503(auth_headers: dict[str, str]) -> None:
    # With no cluster the reader is a NullPodLogReader, which the endpoint turns
    # into a 503-with-reason rather than crashing.
    with _client_with(NullPodLogReader()) as client:
        resp = client.get(URL, headers=auth_headers)
    assert resp.status_code == 503
    assert "no kubernetes cluster" in resp.json()["detail"]


def test_build_reader_degrades_to_null_when_config_unloadable() -> None:
    # An explicit but unloadable kubeconfig path degrades to the null reader.
    reader = build_pod_log_reader("/nonexistent/kubeconfig-path.yaml")
    assert isinstance(reader, NullPodLogReader)


def test_returns_logs_from_the_reader(auth_headers: dict[str, str]) -> None:
    with _client_with(FakeReader()) as client:
        resp = client.get(URL, headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["namespace"] == "preview-pr-1"
    assert body["pod"] == "runner-abc"
    assert body["logs"] == "logs for preview-pr-1/runner-abc"


def test_pod_not_found_maps_to_404(auth_headers: dict[str, str]) -> None:
    with _client_with(NotFoundReader()) as client:
        resp = client.get(URL, headers=auth_headers)
    assert resp.status_code == 404


def test_runner_logs_require_api_key(client: Any) -> None:
    assert client.get(URL).status_code == 401
