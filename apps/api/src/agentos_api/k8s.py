"""Pod-log reader for the per-run runner-logs proxy (OB1).

The reader is a small injectable seam so the endpoint is testable with a fake and
degrades cleanly: when no cluster is configured the reader raises
NoClusterConfigured, which the endpoint turns into a 503 with a reason rather
than crashing. The real implementation wraps the (untyped) kubernetes client.
"""

import logging
from collections.abc import Callable
from typing import Any, Protocol

logger = logging.getLogger(__name__)


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


class _SuppressExecCredentialError(logging.Filter):
    """Drops the kubernetes client's root-logger ERROR for a failed exec plugin.

    Loading a kubeconfig whose user auths via an exec credential plugin (e.g.
    ``aws eks get-token``) makes the kubernetes client log
    ``exec: process returned ...`` at ERROR on the root logger when the plugin
    fails (typically expired AWS/SSO creds). That reads like a crash; we surface a
    single WARN of our own instead, so this filter suppresses only that line.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "exec: process returned" not in record.getMessage()


class LazyPodLogReader:
    """Defers cluster/credential resolution until the first pod-log read.

    Building the real reader loads a kubeconfig, which resolves the user's
    credentials (running any exec plugin). Doing that eagerly at app startup turns
    absent/expired creds into a scary boot-time ERROR for a proxy most runs never
    touch. Resolving lazily keeps startup clean; the (cached) real reader is built
    on the first read, and an absent cluster still degrades to 503 there.
    """

    def __init__(self, factory: Callable[[], PodLogReader]) -> None:
        self._factory = factory
        self._reader: PodLogReader | None = None

    def _resolve(self) -> PodLogReader:
        if self._reader is None:
            self._reader = self._factory()
        return self._reader

    def read(
        self,
        namespace: str,
        pod: str,
        container: str | None,
        tail_lines: int | None,
        previous: bool,
    ) -> str:
        return self._resolve().read(
            namespace, pod, container, tail_lines, previous
        )


def build_pod_log_reader(kube_config_path: str | None) -> PodLogReader:
    """Build a real reader from kubeconfig or in-cluster config, else a null one.

    When no usable cluster/credential is available this logs a single WARN and
    returns a reader that degrades to 503, rather than letting the kubernetes
    client's raw exec-credential ERROR reach the operator.
    """

    root = logging.getLogger()
    exec_filter = _SuppressExecCredentialError()
    root.addFilter(exec_filter)
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
    except Exception as exc:
        logger.warning(
            "runner-logs proxy: no usable kubernetes cluster (%s); "
            "pod-log reads will return 503",
            exc,
        )
        return NullPodLogReader()
    finally:
        root.removeFilter(exec_filter)


def build_lazy_pod_log_reader(kube_config_path: str | None) -> LazyPodLogReader:
    """A pod-log reader that resolves the cluster on first use, not at startup."""

    return LazyPodLogReader(lambda: build_pod_log_reader(kube_config_path))
