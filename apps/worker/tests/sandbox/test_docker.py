"""DockerSandboxClient: the local (no-Kubernetes) SandboxClient.

The ``docker`` CLI is the one external dependency, so it is captured/stubbed; the
argv construction, the MinIO bundle fetch + B2-aligned unwrap, and the inspect
parsing (port + operating mode) are exercised for real.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

from agentos_worker.bundle_store import extract_bundle
from agentos_worker.sandbox.docker import DockerSandboxClient


class _FakeBundleStore:
    def __init__(self, data: bytes = b"") -> None:
        self._data = data
        self.requested: list[str] = []

    def get(self, key: str) -> bytes:
        self.requested.append(key)
        return self._data


class _RecordingDocker(DockerSandboxClient):
    """Captures every docker argv and returns canned stdout per subcommand."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.calls: list[list[str]] = []
        self.outputs: dict[str, str] = {}

    def _docker(self, args: list[str], *, check: bool = True) -> str:
        self.calls.append(args)
        return self.outputs.get(args[0], "")


def _plugin_tar_gz(wrapper: str | None) -> bytes:
    """A tar.gz carrying a Claude Code plugin. When ``wrapper`` is set, the
    manifest sits one level down (the common ``tar czf b.tgz myplugin/`` shape)."""
    buf = io.BytesIO()
    prefix = f"{wrapper}/" if wrapper else ""
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        manifest = b'{"name": "deal-desk", "version": "0.1.0"}'
        info = tarfile.TarInfo(f"{prefix}.claude-plugin/plugin.json")
        info.size = len(manifest)
        tf.addfile(info, io.BytesIO(manifest))
    return buf.getvalue()


def _flag_values(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag and i + 1 < len(argv)]


def test_create_claim_argv_carries_boot_env() -> None:
    client = _RecordingDocker(
        image="agentos-runner",
        bundle_store=_FakeBundleStore(),
        network="agentos_default",
        otel_endpoint="http://otel:4318",
    )
    client.create_claim(
        "thread-abc",
        pool="pool",
        env={
            "AGENTOS_BUDGET": '{"max_usd_per_day":5.0}',
            "AGENTOS_SESSION_ID": "sess-1",
            "AGENTOS_FAKE_MODEL": "1",
            "AGENTOS_PLUGIN_DIR": "/bundles/current",
        },
        labels={"agentos.dev/thread-hash": "abc"},
    )
    argv = client.calls[0]
    joined = " ".join(argv)
    assert joined.startswith("run -d --name thread-abc")
    assert "127.0.0.1::8080" in _flag_values(argv, "-p")
    assert "agentos_default" in _flag_values(argv, "--network")
    labels = _flag_values(argv, "--label")
    assert "agentos.dev/managed-by=agentos-sandbox-substrate" in labels
    assert "agentos.dev/thread-hash=abc" in labels
    envs = _flag_values(argv, "-e")
    assert "AGENTOS_PLUGIN_DIR=/bundles/current" in envs
    assert "AGENTOS_SANDBOX_ID=thread-abc" in envs
    assert "AGENTOS_RUNNER_PORT=8080" in envs
    assert 'AGENTOS_BUDGET={"max_usd_per_day":5.0}' in envs
    assert "AGENTOS_FAKE_MODEL=1" in envs
    assert "OTEL_EXPORTER_OTLP_ENDPOINT=http://otel:4318" in envs
    assert argv[-1] == "agentos-runner"


def test_create_claim_fetches_and_unwraps_bundle() -> None:
    store = _FakeBundleStore(_plugin_tar_gz(wrapper="deal-desk"))
    client = _RecordingDocker(image="agentos-runner", bundle_store=store)
    client.create_claim(
        "t1",
        pool="pool",
        env={"AGENTOS_BUNDLE_REF": "bundles/b.tar.gz", "AGENTOS_PLUGIN_DIR": "/bundles/current"},
    )
    assert store.requested == ["bundles/b.tar.gz"]  # the worker fetched the bundle
    mounts = _flag_values(client.calls[0], "-v")
    assert len(mounts) == 1
    host_dir, container_dir, mode = mounts[0].split(":")
    assert container_dir == "/bundles/current"
    assert mode == "ro"
    # Unwrapped: the manifest sits at the mount root, not under the wrapper dir.
    assert (Path(host_dir) / ".claude-plugin" / "plugin.json").is_file()
    # The MinIO object key itself is not forwarded into the container env.
    assert all("AGENTOS_BUNDLE_REF" not in e for e in _flag_values(client.calls[0], "-e"))


def test_create_claim_without_bundle_ref_mounts_nothing() -> None:
    client = _RecordingDocker(image="agentos-runner", bundle_store=_FakeBundleStore())
    client.create_claim("t1", pool="pool", env={"AGENTOS_FAKE_MODEL": "1"})
    assert _flag_values(client.calls[0], "-v") == []


def test_sdk_credential_forwarded_by_name_only() -> None:
    # A real-model run: the SDK token is present in the worker env. It must be
    # forwarded by NAME (docker reads the value), never with its value in the argv.
    client = _RecordingDocker(
        image="agentos-runner",
        bundle_store=_FakeBundleStore(),
        environ={"CLAUDE_CODE_OAUTH_TOKEN": "sk-PLACEHOLDER-never-real"},
    )
    client.create_claim("t1", pool="pool", env={"AGENTOS_BUDGET": "{}"})
    argv = client.calls[0]
    envs = _flag_values(argv, "-e")
    assert "CLAUDE_CODE_OAUTH_TOKEN" in envs  # forwarded by name
    assert all("PLACEHOLDER" not in a for a in argv)  # the value never leaks in


def test_sdk_credential_not_forwarded_when_absent() -> None:
    client = _RecordingDocker(
        image="agentos-runner", bundle_store=_FakeBundleStore(), environ={}
    )
    client.create_claim("t1", pool="pool", env={"AGENTOS_FAKE_MODEL": "1"})
    envs = _flag_values(client.calls[0], "-e")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in envs
    assert "ANTHROPIC_API_KEY" not in envs


def test_get_sandbox_reports_published_port_and_mode() -> None:
    client = _RecordingDocker(image="agentos-runner", bundle_store=_FakeBundleStore())
    client.outputs = {
        "inspect": "running\t{}",
        "port": "127.0.0.1:49173\n",
    }
    view = client.get_sandbox("t1")
    assert view is not None
    assert view.service_fqdn == "127.0.0.1"
    assert view.port == 49173
    assert view.operating_mode == "Running"

    client.outputs["inspect"] = "paused\t{}"
    paused = client.get_sandbox("t1")
    assert paused is not None
    assert paused.operating_mode == "Suspended"


def test_get_sandbox_none_when_container_absent() -> None:
    client = _RecordingDocker(image="agentos-runner", bundle_store=_FakeBundleStore())
    client.outputs = {"inspect": ""}  # docker inspect on a missing container
    assert client.get_sandbox("gone") is None


def test_get_sandbox_treats_dead_container_as_gone() -> None:
    # An exited/dead/created container is NOT a live sandbox: report it gone so
    # the substrate evicts the stale route instead of dialing a dead runner.
    client = _RecordingDocker(image="agentos-runner", bundle_store=_FakeBundleStore())
    for dead in ("exited", "dead", "created", "restarting"):
        client.outputs = {"inspect": f"{dead}\t{{}}", "port": ""}
        assert client.get_sandbox("t1") is None, dead


def test_extract_bundle_unwraps_single_wrapper_dir() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = extract_bundle(_plugin_tar_gz(wrapper="deal-desk"), Path(tmp))
        assert (root / ".claude-plugin" / "plugin.json").is_file()

    with tempfile.TemporaryDirectory() as tmp:
        # A flat bundle (manifest already at the archive root) is not descended.
        root = extract_bundle(_plugin_tar_gz(wrapper=None), Path(tmp))
        assert root == Path(tmp)
        assert (root / ".claude-plugin" / "plugin.json").is_file()
