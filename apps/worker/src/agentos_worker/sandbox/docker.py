"""Docker access for the sandbox substrate: local middle mode, no Kubernetes.

``DockerSandboxClient`` implements the same ``SandboxClient`` seam
(``apps/worker/src/agentos_worker/sandbox/k8s.py``) that ``SandboxSubstrate`` is
written against, so selecting it (``AGENTOS_SANDBOX_SUBSTRATE=docker``) makes the
whole worker -- both the runs kernel and the eval consumer, which share one
substrate object -- boot runner containers with ``docker run`` instead of
claiming agent-sandbox CRs. The boot recipe mirrors the CLI's
``cli/src/docker.rs`` (image ``agentos-runner``, port publish, plugin mount,
network join, the ACI boot env).

Model mapping. Docker has no warm pool, no separate Sandbox object, and no init
containers, so one container is simultaneously the "claim" and the "sandbox"
(claim name == container name == sandbox name), and there is no bundle-fetch init
pair -- the worker fetches the plugin bundle from MinIO and bind-mounts it. The
host-process worker reaches each runner over a loopback-bound, Docker-assigned
host port (``docker port <name> 8080``); the container additionally joins the
compose network when one is configured, so it can reach the OTel collector by
name. Readiness (the substrate's ``claim.ready`` gate) is the runner's own
``/healthz`` answering, so ``claim()`` never returns a handle to a not-yet-listening
runner. Suspend maps to ``docker pause`` (the process freezes but keeps its port);
the substrate's resume path retires the paused container and claims a fresh one.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from ..binding import BUNDLE_REF_ENV, PLUGIN_DIR_ENV
from ..bundle_store import BundleStore, extract_bundle
from .k8s import (
    MANAGED_BY_LABEL,
    MANAGED_BY_VALUE,
    OperatingMode,
)
from .types import ClaimView, SandboxView

logger = logging.getLogger(__name__)

# The runner listens on this fixed port inside every container; the host port it
# is published to is Docker-assigned and read back per-container.
RUNNER_CONTAINER_PORT = 8080
# Env keys the worker owns on the Docker path and must not blindly forward into
# the container: the plugin dir and sandbox id are set explicitly, and the bundle
# ref named a MinIO object the worker already fetched (the runner never fetches).
_WORKER_OWNED_ENV = frozenset({BUNDLE_REF_ENV, PLUGIN_DIR_ENV, "AGENTOS_SANDBOX_ID"})


class DockerError(Exception):
    """A ``docker`` CLI invocation failed."""


class DockerSandboxClient:
    """SandboxClient backed by local Docker containers (no Kubernetes)."""

    def __init__(
        self,
        *,
        image: str,
        bundle_store: BundleStore,
        network: str | None = None,
        otel_endpoint: str | None = None,
        host: str = "127.0.0.1",
        default_plugin_dir: str = "/bundles/current",
        healthz_timeout_s: float = 0.5,
    ) -> None:
        self._image = image
        self._bundles = bundle_store
        self._network = network
        self._otel_endpoint = otel_endpoint
        self._host = host
        self._default_plugin_dir = default_plugin_dir
        self._healthz_timeout_s = healthz_timeout_s
        # Per-container host dir holding the extracted bundle, cleaned on delete.
        self._bundle_dirs: dict[str, str] = {}

    # -- claim lifecycle ------------------------------------------------------

    def create_claim(
        self,
        name: str,
        *,
        pool: str,  # noqa: ARG002 -- no warm pool in Docker; kept for the seam.
        env: dict[str, str] | None = None,
        labels: dict[str, str] | None = None,
    ) -> None:
        env = dict(env or {})
        plugin_dir = env.get(PLUGIN_DIR_ENV, self._default_plugin_dir)
        args = [
            "run",
            "-d",
            "--name",
            name,
            "--label",
            f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE}",
            "-p",
            f"{self._host}::{RUNNER_CONTAINER_PORT}",
        ]
        for key, value in (labels or {}).items():
            args += ["--label", f"{key}={value}"]
        if self._network:
            args += ["--network", self._network]

        # Bundle: no init containers here, so fetch + extract + bind-mount the
        # plugin dir ourselves (fail-open only when there is no ref, e.g. a fake
        # model run that never reads the plugin dir).
        if env.get(BUNDLE_REF_ENV):
            root = self._prepare_bundle(name, env[BUNDLE_REF_ENV])
            args += ["-v", f"{root}:{plugin_dir}:ro"]

        args += [
            "-e",
            f"{PLUGIN_DIR_ENV}={plugin_dir}",
            "-e",
            f"AGENTOS_SANDBOX_ID={name}",
            "-e",
            f"AGENTOS_RUNNER_PORT={RUNNER_CONTAINER_PORT}",
        ]
        if self._otel_endpoint:
            args += [
                "-e",
                f"OTEL_EXPORTER_OTLP_ENDPOINT={self._otel_endpoint}",
                "-e",
                "OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf",
            ]
        for key, value in sorted(env.items()):
            if key not in _WORKER_OWNED_ENV:
                args += ["-e", f"{key}={value}"]
        args.append(self._image)

        try:
            self._docker(args)
        except DockerError:
            # A failed boot must not leak the bundle dir we just staged.
            self._cleanup_bundle(name)
            raise

    def get_claim(self, name: str) -> ClaimView | None:
        inspected = self._inspect(name)
        if inspected is None:
            return None
        status, labels = inspected
        ready = status == "running" and self._healthz_ok(name)
        return ClaimView(
            name=name,
            ready=ready,
            # The container is its own sandbox; expose the name once it exists so
            # the substrate can advance to awaiting the dial target.
            sandbox_name=name,
            labels=labels,
        )

    def delete_claim(self, name: str) -> None:
        # -f removes a running/paused container too; ignore "no such container".
        self._docker(["rm", "-f", name], check=False)
        self._cleanup_bundle(name)

    def list_claims(self, *, label_selector: str) -> list[ClaimView]:
        out = self._docker(
            [
                "ps",
                "-a",
                "--filter",
                f"label={label_selector}",
                "--format",
                "{{.Names}}",
            ]
        )
        names = [line.strip() for line in out.splitlines() if line.strip()]
        views: list[ClaimView] = []
        for cname in names:
            view = self.get_claim(cname)
            if view is not None:
                views.append(view)
        return views

    # -- sandbox lifecycle ----------------------------------------------------

    def get_sandbox(self, name: str) -> SandboxView | None:
        inspected = self._inspect(name)
        if inspected is None:
            return None
        status, _labels = inspected
        port = self._published_port(name)
        return SandboxView(
            name=name,
            ready=status == "running",
            # Dial target is ready only once the host port is published.
            service_fqdn=self._host if port is not None else None,
            operating_mode="Suspended" if status == "paused" else "Running",
            port=port,
        )

    def set_sandbox_mode(self, name: str, mode: OperatingMode) -> None:
        # Docker has no cold suspend; pause freezes the process while keeping the
        # published port, which is all the substrate's liveness check reads back.
        verb = "pause" if mode == "Suspended" else "unpause"
        self._docker([verb, name], check=False)

    # -- helpers --------------------------------------------------------------

    def _prepare_bundle(self, name: str, ref: str) -> str:
        data = self._bundles.get(ref)
        tmp = tempfile.mkdtemp(prefix="agentos-bundle-")
        try:
            root = extract_bundle(data, Path(tmp))
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        self._bundle_dirs[name] = tmp
        return str(root)

    def _cleanup_bundle(self, name: str) -> None:
        tmp = self._bundle_dirs.pop(name, None)
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)

    def _inspect(self, name: str) -> tuple[str, dict[str, str]] | None:
        """(status, labels) for the container, or None when it does not exist."""
        out = self._docker(
            ["inspect", "--format", "{{.State.Status}}\t{{json .Config.Labels}}", name],
            check=False,
        )
        if not out.strip():
            return None
        status, _, labels_json = out.strip().partition("\t")
        labels_raw = json.loads(labels_json) if labels_json and labels_json != "null" else {}
        labels = {str(k): str(v) for k, v in labels_raw.items()}
        return status, labels

    def _published_port(self, name: str) -> int | None:
        out = self._docker(
            ["port", name, f"{RUNNER_CONTAINER_PORT}/tcp"], check=False
        )
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            # e.g. "127.0.0.1:49153" or "0.0.0.0:49153"; take the trailing port.
            _, _, port = line.rpartition(":")
            if port.isdigit():
                return int(port)
        return None

    def _healthz_ok(self, name: str) -> bool:
        port = self._published_port(name)
        if port is None:
            return False
        url = f"http://{self._host}:{port}/healthz"
        try:
            with urllib.request.urlopen(url, timeout=self._healthz_timeout_s) as resp:
                return bool(200 <= resp.status < 300)
        except (urllib.error.URLError, OSError):
            return False

    def _docker(self, args: list[str], *, check: bool = True) -> str:
        proc = subprocess.run(  # noqa: S603 -- fixed argv, no shell.
            ["docker", *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            if check:
                # Never echo args: they may carry AGENTOS_CREDENTIALS.
                raise DockerError(
                    f"docker {args[0]} failed ({proc.returncode}): {proc.stderr.strip()}"
                )
            return ""
        return proc.stdout
