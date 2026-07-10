"""Docker access for the sandbox substrate: local middle mode, no Kubernetes.

``DockerSandboxClient`` implements the same ``SandboxClient`` seam
(``apps/worker/src/agentos_worker/sandbox/k8s.py``) that ``SandboxSubstrate`` is
written against, so selecting it (``AGENTOS_SANDBOX_SUBSTRATE=docker``) makes the
whole worker -- both the runs kernel and the eval consumer, which share one
substrate object -- boot runner containers with ``docker run`` instead of
claiming agent-sandbox CRs. The boot recipe mirrors the CLI's
``cli/src/docker.rs`` (image ``agentos-runner``, port publish, plugin mount,
network join, the ACI boot env).

Local middle mode defaults to a REAL model: the runner authenticates through the
claude-agent-sdk, which reads ``CLAUDE_CODE_OAUTH_TOKEN`` / ``ANTHROPIC_API_KEY``
from its own environment (the runner never dereferences the ACI
``AGENTOS_CREDENTIALS`` reference). So this client forwards those SDK credential
vars BY NAME into every runner container when the worker has them set -- Docker
reads the value from the worker's environment, this code never does. Fake model
is an explicit offline/test opt-in (``AGENTOS_FAKE_MODEL=1``), gated in
``run.py``: middle mode never silently degrades to a fake when a credential is
missing.

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
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path

from ..binding import BUNDLE_REF_ENV, CREDENTIALS_ENV, PLUGIN_DIR_ENV
from ..bundle_store import BundleStore, extract_bundle
from .k8s import (
    MANAGED_BY_LABEL,
    MANAGED_BY_VALUE,
    OperatingMode,
)
from .types import ClaimView, SandboxError, SandboxView

logger = logging.getLogger(__name__)

# The runner listens on this fixed port inside every container; the host port it
# is published to is Docker-assigned and read back per-container.
RUNNER_CONTAINER_PORT = 8080

# The ambient SDK credential vars, forwarded into the container BY NAME (docker
# reads the value from the worker env; this code never does, and no secret ever
# lands in the docker argv). These authenticate the runner directly on the legacy
# real-Anthropic path, and are forwarded only when no explicit AGENTOS_CREDENTIALS
# is chosen. AGENTOS_CREDENTIALS (the ACI reference the runner maps onto an SDK
# var) is selected positively and alone. Mirrors the CLI (cli/src/docker.rs,
# cli/src/commands.rs).
_SDK_PASSTHROUGH_ENV = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
)
# Env keys the worker sets explicitly or forwards specially, so the generic
# value loop must not also emit them: the plugin dir and sandbox id are set
# explicitly, the bundle ref named a MinIO object the worker already fetched
# (the runner never fetches), and the credential is forwarded by name (never as
# a value in the argv).
_WORKER_OWNED_ENV = frozenset(
    {BUNDLE_REF_ENV, PLUGIN_DIR_ENV, "AGENTOS_SANDBOX_ID", CREDENTIALS_ENV}
)


class DockerError(SandboxError):
    """A ``docker`` CLI invocation failed.

    Subclasses ``SandboxError`` so a provisioning failure (missing runner image,
    Docker daemon down) is handled by the kernel's runner-error retry path and
    the eval consumer's provisioning-failure path, rather than escaping unhandled
    and looping on reclaim.
    """


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
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._image = image
        self._bundles = bundle_store
        self._network = network
        self._otel_endpoint = otel_endpoint
        self._host = host
        self._default_plugin_dir = default_plugin_dir
        self._healthz_timeout_s = healthz_timeout_s
        # Presence-only view of the worker environment, used to decide which SDK
        # credential vars to forward by name. Never read for their values.
        self._environ = environ if environ is not None else os.environ
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
        # Forward exactly one model credential into the runner, always BY NAME (docker
        # reads the value from the worker env; the secret never lands in the argv). An
        # explicit AGENTOS_CREDENTIALS is the operator's chosen BYO credential and is
        # forwarded alone, so an ambient SDK token (leaked into the worker via compose's
        # .env auto-load or the operator shell) can neither shadow it nor ride into the
        # sandbox. With no BYO credential set, fall back to the ambient SDK credential(s)
        # for the legacy real-Anthropic path. Selection keys on the worker environ (the
        # by-name forward reads the value from there); an empty AGENTOS_CREDENTIALS is
        # treated as unset.
        if self._environ.get(CREDENTIALS_ENV):
            args += ["-e", CREDENTIALS_ENV]
        else:
            for var in _SDK_PASSTHROUGH_ENV:
                if var in self._environ:
                    args += ["-e", var]
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
        # Only a running or paused container is a live/suspended sandbox. An
        # exited/dead/created/restarting container is NOT live: report it gone
        # (None) so the substrate evicts the stale route and re-claims rather than
        # handing back a route that keeps dialing a dead runner until TTL expiry.
        if status == "paused":
            operating_mode = "Suspended"
        elif status == "running":
            operating_mode = "Running"
        else:
            return None
        port = self._published_port(name)
        return SandboxView(
            name=name,
            ready=status == "running",
            # Dial target is ready only once the host port is published.
            service_fqdn=self._host if port is not None else None,
            operating_mode=operating_mode,
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
            # The mount is read-only and the runner is non-root (uid 1000), so the
            # staged bundle must be group/other readable+traversable; mkdtemp
            # defaults to 0700, which the non-root runner cannot enter.
            os.chmod(tmp, 0o755)
            for dirpath, dirnames, filenames in os.walk(tmp):
                for d in dirnames:
                    os.chmod(os.path.join(dirpath, d), 0o755)
                for f in filenames:
                    p = os.path.join(dirpath, f)
                    os.chmod(p, os.stat(p).st_mode | 0o044)
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

    def ensure_image(self) -> None:
        """Pre-pull the runner image if absent (IfNotPresent semantics).

        Mirrors the K8s prewarm: run once at worker startup so the first claim
        window never contains a cold image download. Best-effort -- a pull
        failure (offline, registry down) warns and continues rather than
        crashing the worker, and a truly-missing image still fails clearly
        later at claim time.
        """
        try:
            present = self._docker(["image", "inspect", self._image], check=False)
            if present.strip():
                return
            self._docker(["pull", self._image])
        except (DockerError, OSError):
            # Log the image name only -- never the argv or stderr (house rule
            # above: args may carry credentials).
            logger.warning(
                "runner image pre-pull failed for %s; first claim will pull implicitly",
                self._image,
            )
