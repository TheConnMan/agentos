"""Runner-logs proxy: no-cluster degradation, success, and error mapping.

The K8s access is injected, so success and error paths are exercised with a fake
reader; the default app (no cluster configured) proves the 503 degradation.
"""

import logging
from typing import Any

import pytest
from agentos_api.deps import get_pod_log_reader
from agentos_api.k8s import (
    LazyPodLogReader,
    NullPodLogReader,
    PodLogError,
    PodLogReader,
    build_pod_log_reader,
)
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


def test_lazy_reader_defers_resolution_until_first_read() -> None:
    # The lazy reader must not build the real reader (which loads a kubeconfig and
    # resolves credentials) until the first read is actually served -- that is
    # what keeps app startup free of the exec-credential ERROR.
    calls = {"n": 0}

    def factory() -> PodLogReader:
        calls["n"] += 1
        return FakeReader()

    reader = LazyPodLogReader(factory)
    assert calls["n"] == 0  # constructing it resolves nothing

    first = reader.read("ns", "pod", None, None, False)
    assert first == "logs for ns/pod"
    assert calls["n"] == 1  # resolved on first read

    reader.read("ns", "pod2", None, None, False)
    assert calls["n"] == 1  # cached: not rebuilt per read


def test_build_reader_warns_not_errors_when_creds_absent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # An unloadable/credential-less cluster config must degrade with a single WARN
    # from our code, never an ERROR (the scary boot log the audit flagged).
    with caplog.at_level(logging.WARNING):
        reader = build_pod_log_reader("/nonexistent/kubeconfig-path.yaml")
    assert isinstance(reader, NullPodLogReader)
    records = [r for r in caplog.records if r.name == "agentos_api.k8s"]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


def test_app_startup_emits_no_exec_credential_error(
    _disposable_db: Any, caplog: pytest.LogCaptureFixture
) -> None:
    # Booting the app must not eagerly resolve the cluster: on a host whose
    # kubeconfig auths via an exec plugin with expired creds, eager init logged
    # `exec: process returned ...` at ERROR at startup. With lazy init the
    # lifespan is clean; any exec-credential noise is deferred to a real pod-log
    # read (and even then suppressed in favor of a single WARN).
    with caplog.at_level(logging.DEBUG):
        with TestClient(create_app()):
            pass
    offenders = [
        r
        for r in caplog.records
        if r.levelno >= logging.ERROR and "exec: process returned" in r.getMessage()
    ]
    assert offenders == [], f"startup logged exec-credential error(s): {offenders}"
