"""Pod-log reader for the per-run runner-logs proxy (OB1).

The reader is a small injectable seam so the endpoint is testable with a fake and
degrades cleanly: when no cluster is configured the reader raises
NoClusterConfigured, which the endpoint turns into a 503 with a reason rather
than crashing. The real implementation wraps the (untyped) kubernetes client.
"""

from typing import Any, Protocol


class NoClusterConfigured(Exception):
    """No kubernetes cluster is configured for the runner-logs proxy."""


class PodLogError(Exception):
    """The cluster rejected the pod-log read; status mirrors the K8s API."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class PodLogReader(Protocol):
    def read(
        self,
        namespace: str,
        pod: str,
        container: str | None,
        tail_lines: int | None,
        previous: bool,
    ) -> str: ...


class NullPodLogReader:
    """Used when no cluster is configured; every read degrades to 503."""

    def read(
        self,
        namespace: str,
        pod: str,
        container: str | None,
        tail_lines: int | None,
        previous: bool,
    ) -> str:
        raise NoClusterConfigured(
            "no kubernetes cluster configured for runner logs"
        )


class KubernetesPodLogReader:
    """Reads pod logs via the kubernetes CoreV1 API (client is untyped -> Any)."""

    def __init__(self, core_v1: Any) -> None:
        self._core_v1 = core_v1

    def read(
        self,
        namespace: str,
        pod: str,
        container: str | None,
        tail_lines: int | None,
        previous: bool,
    ) -> str:
        try:
            # _preload_content=False returns the raw HTTPResponse; decoding its
            # bytes ourselves avoids the kubernetes client's str(bytes) quirk
            # that otherwise yields a b'...' repr for the log text.
            response = self._core_v1.read_namespaced_pod_log(
                name=pod,
                namespace=namespace,
                container=container,
                tail_lines=tail_lines,
                previous=previous,
                _preload_content=False,
            )
            logs: str = response.data.decode("utf-8", "replace")
            return logs
        except Exception as exc:  # kubernetes ApiException carries .status
            status = getattr(exc, "status", None)
            raise PodLogError(
                str(exc), status if isinstance(status, int) else None
            ) from exc


def build_pod_log_reader(kube_config_path: str | None) -> PodLogReader:
    """Build a real reader from kubeconfig or in-cluster config, else a null one."""

    try:
        from kubernetes import client, config

        if kube_config_path:
            config.load_kube_config(config_file=kube_config_path)
        else:
            try:
                config.load_incluster_config()
            except Exception:
                # Not in a cluster: honor the standard KUBECONFIG / ~/.kube/config
                # so local runs against a real cluster work without extra config.
                config.load_kube_config()
        return KubernetesPodLogReader(client.CoreV1Api())
    except Exception:
        return NullPodLogReader()
