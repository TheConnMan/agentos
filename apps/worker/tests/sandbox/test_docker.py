"""DockerSandboxClient: the local (no-Kubernetes) SandboxClient.

The ``docker`` CLI is the one external dependency, so it is captured/stubbed; the
argv construction, the MinIO bundle fetch + B2-aligned unwrap, and the inspect
parsing (port + operating mode) are exercised for real.
"""

from __future__ import annotations

import io
import logging
import tarfile
from pathlib import Path

from agentos_worker.bundle_store import extract_bundle
from agentos_worker.sandbox.docker import (
    RUNNER_CONTAINER_PORT,
    DockerError,
    DockerSandboxClient,
)


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
    client = _RecordingDocker(image="agentos-runner", bundle_store=_FakeBundleStore(), environ={})
    client.create_claim("t1", pool="pool", env={"AGENTOS_FAKE_MODEL": "1"})
    envs = _flag_values(client.calls[0], "-e")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in envs
    assert "ANTHROPIC_API_KEY" not in envs


def test_agentos_credentials_forwarded_by_name_never_as_value() -> None:
    # The ACI credential reference must reach the runner (which maps it) without
    # its value ever appearing in the docker argv.
    client = _RecordingDocker(
        image="agentos-runner",
        bundle_store=_FakeBundleStore(),
        environ={"AGENTOS_CREDENTIALS": "sk-ant-PLACEHOLDER-secret"},
    )
    # The worker also hands it in the boot env (from config.credentials); it must
    # still never be emitted as a -e KEY=value pair.
    client.create_claim("t1", pool="pool", env={"AGENTOS_CREDENTIALS": "sk-ant-PLACEHOLDER-secret"})
    argv = client.calls[0]
    envs = _flag_values(argv, "-e")
    assert "AGENTOS_CREDENTIALS" in envs  # forwarded by name
    assert all("PLACEHOLDER-secret" not in a for a in argv)  # value never in argv
    assert "AGENTOS_CREDENTIALS=sk-ant-PLACEHOLDER-secret" not in envs


def test_ambient_sdk_creds_not_forwarded_when_agentos_credentials_set() -> None:
    # BYO model: an explicit AGENTOS_CREDENTIALS is present, and the operator's
    # shell also carries ambient SDK tokens. Positive selection forwards exactly
    # AGENTOS_CREDENTIALS by name and does NOT forward the ambient SDK vars (which
    # would re-inject the operator's token and shadow the BYO credential in the
    # runner).
    client = _RecordingDocker(
        image="agentos-runner",
        bundle_store=_FakeBundleStore(),
        environ={
            "CLAUDE_CODE_OAUTH_TOKEN": "sk-PLACEHOLDER-oauth",
            "ANTHROPIC_API_KEY": "sk-ant-PLACEHOLDER-key",
            "AGENTOS_CREDENTIALS": "sk-or-PLACEHOLDER-byo",
        },
    )
    client.create_claim("t1", pool="pool", env={"AGENTOS_BUDGET": "{}"})
    argv = client.calls[0]
    envs = _flag_values(argv, "-e")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in envs  # ambient OAuth token not forwarded by name
    assert "ANTHROPIC_API_KEY" not in envs  # ambient API key not forwarded by name
    assert "AGENTOS_CREDENTIALS" in envs  # explicit BYO reference still forwarded by name
    assert all("PLACEHOLDER" not in a for a in argv)  # no secret value leaks into argv


def test_ambient_sdk_creds_forwarded_when_agentos_credentials_empty() -> None:
    # Legacy real-model path: an empty AGENTOS_CREDENTIALS placeholder (a blank
    # line in compose's .env) must NOT suppress a real ambient OAuth token.
    # Positive selection keys on VALUE-truthiness, not key-presence, so an empty
    # AGENTOS_CREDENTIALS is treated as absent and the ambient SDK var is forwarded.
    client = _RecordingDocker(
        image="agentos-runner",
        bundle_store=_FakeBundleStore(),
        environ={
            "AGENTOS_CREDENTIALS": "",
            "CLAUDE_CODE_OAUTH_TOKEN": "sk-PLACEHOLDER-oauth",
        },
    )
    client.create_claim("t1", pool="pool", env={"AGENTOS_BUDGET": "{}"})
    argv = client.calls[0]
    envs = _flag_values(argv, "-e")
    assert "CLAUDE_CODE_OAUTH_TOKEN" in envs  # ambient OAuth token forwarded by name
    assert all("PLACEHOLDER" not in a for a in argv)  # the value never leaks into argv


# Placeholder ambient OAuth token (never real). Hoisted into a named constant so
# the secrets-scan pre-commit hook does not false-positive on an inline
# `"CLAUDE_CODE_OAUTH_TOKEN": "sk-..."` literal; the value is asserted absent from
# the forwarded argv below.
_AMBIENT_OAUTH = "sk-PLACEHOLDER-oauth"


def test_no_credential_forwarded_under_fake_model() -> None:
    # A fake-model run needs no model credential: neither the explicit BYO
    # reference nor the ambient SDK token must ride into the untrusted runner.
    # Gating keys on the boot env (AGENTOS_FAKE_MODEL present), not the worker
    # environ, so even a fully-credentialed worker leaks nothing.
    client = _RecordingDocker(
        image="agentos-runner",
        bundle_store=_FakeBundleStore(),
        environ={
            "CLAUDE_CODE_OAUTH_TOKEN": _AMBIENT_OAUTH,
            "AGENTOS_CREDENTIALS": "sk-ant-PLACEHOLDER",
        },
    )
    client.create_claim("t1", pool="pool", env={"AGENTOS_FAKE_MODEL": "1", "AGENTOS_BUDGET": "{}"})
    envs = _flag_values(client.calls[0], "-e")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in envs  # ambient SDK token not forwarded
    assert "ANTHROPIC_API_KEY" not in envs
    assert "AGENTOS_CREDENTIALS" not in envs  # BYO reference not forwarded either
    assert all("PLACEHOLDER" not in a for a in client.calls[0])  # no value leaks


def test_ambient_sdk_creds_not_forwarded_under_local_model() -> None:
    # A local/base-URL-override run (ANTHROPIC_BASE_URL in the boot env) points the
    # runner at a local endpoint that needs no Anthropic credential, so the ambient
    # SDK token must not ride into that container.
    client = _RecordingDocker(
        image="agentos-runner",
        bundle_store=_FakeBundleStore(),
        environ={"CLAUDE_CODE_OAUTH_TOKEN": _AMBIENT_OAUTH},
    )
    client.create_claim(
        "t1",
        pool="pool",
        env={"ANTHROPIC_BASE_URL": "http://ollama:11434", "AGENTOS_BUDGET": "{}"},
    )
    envs = _flag_values(client.calls[0], "-e")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in envs  # ambient SDK token not forwarded
    assert "ANTHROPIC_API_KEY" not in envs
    assert all("PLACEHOLDER" not in a for a in client.calls[0])  # no value leaks


def test_explicit_credential_forwarded_under_base_url_override() -> None:
    # BYO OpenRouter with a preset base URL: the runner routes an sk-or- key into
    # ANTHROPIC_API_KEY even when ANTHROPIC_BASE_URL is set (runner sdk_auth), so an
    # EXPLICIT AGENTOS_CREDENTIALS must still be forwarded by name -- while the
    # ambient SDK token still must not ride along.
    client = _RecordingDocker(
        image="agentos-runner",
        bundle_store=_FakeBundleStore(),
        environ={
            "CLAUDE_CODE_OAUTH_TOKEN": _AMBIENT_OAUTH,
            "AGENTOS_CREDENTIALS": "sk-or-PLACEHOLDER",
        },
    )
    client.create_claim(
        "t1",
        pool="pool",
        env={"ANTHROPIC_BASE_URL": "https://openrouter.ai/api", "AGENTOS_BUDGET": "{}"},
    )
    envs = _flag_values(client.calls[0], "-e")
    assert "AGENTOS_CREDENTIALS" in envs  # BYO OpenRouter key still forwarded by name
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in envs  # ambient SDK token still suppressed
    assert "ANTHROPIC_API_KEY" not in envs
    assert all("PLACEHOLDER" not in a for a in client.calls[0])  # value never in argv


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


class _NetworkAwareDocker(DockerSandboxClient):
    """Fake that distinguishes the status inspect from the networks inspect (both
    are ``docker inspect``, so keying on the subcommand alone is not enough)."""

    def __init__(self, *, networks_json: str, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._networks_json = networks_json

    def _docker(self, args: list[str], *, check: bool = True) -> str:
        if args and args[0] == "inspect":
            if any("NetworkSettings.Networks" in a for a in args):
                return self._networks_json
            return "running\t{}"
        if args and args[0] == "port":
            return "127.0.0.1:49173\n"
        return ""


def test_get_sandbox_dials_container_ip_on_shared_network() -> None:
    # A host-net worker container on a Docker Desktop VM cannot reach the runner's
    # host-loopback publish, but can reach its shared-network container IP. So the
    # dial target is that IP at the fixed container port, not the host port.
    client = _NetworkAwareDocker(
        image="agentos-runner",
        bundle_store=_FakeBundleStore(),
        network="agentos_default",
        networks_json='{"agentos_default": {"IPAddress": "172.20.0.11"}}',
    )
    view = client.get_sandbox("t1")
    assert view is not None
    assert view.service_fqdn == "172.20.0.11"
    assert view.port == RUNNER_CONTAINER_PORT  # not the Docker-assigned host port
    assert view.operating_mode == "Running"


def test_get_sandbox_falls_back_to_published_port_without_network_ip() -> None:
    # No shared-network IP yet (or no network): dial the Docker-assigned host
    # loopback port, preserving the host-process worker path.
    client = _NetworkAwareDocker(
        image="agentos-runner",
        bundle_store=_FakeBundleStore(),
        network="agentos_default",
        networks_json="{}",
    )
    view = client.get_sandbox("t1")
    assert view is not None
    assert view.service_fqdn == "127.0.0.1"
    assert view.port == 49173


def test_prepare_bundle_is_readable_by_nonroot_runner() -> None:
    # The staged bundle is bind-mounted :ro into a runner running as uid 1000, so
    # the tree must be group/other traversable+readable. mkdtemp defaults to 0700,
    # which the non-root runner cannot enter -- _prepare_bundle must widen it.
    client = _RecordingDocker(
        image="agentos-runner", bundle_store=_FakeBundleStore(_plugin_tar_gz(wrapper=None))
    )
    root = Path(client._prepare_bundle("t1", "bundles/b.tar.gz"))
    nested_dir = root / ".claude-plugin"
    nested_file = nested_dir / "plugin.json"

    assert root.stat().st_mode & 0o005 == 0o005  # root dir: o+rx
    assert nested_dir.stat().st_mode & 0o005 == 0o005  # nested dir: o+rx
    assert nested_file.stat().st_mode & 0o004 == 0o004  # nested file: o+r


def test_runner_token_forwarded_as_docker_env() -> None:
    # The per-sandbox runner token rides the generic claim-env loop (it is not a
    # worker-owned or credential var), so it must be emitted as -e KEY=value.
    client = _RecordingDocker(image="agentos-runner", bundle_store=_FakeBundleStore())
    client.create_claim(
        "t1",
        pool="pool",
        env={"AGENTOS_BUDGET": "{}", "AGENTOS_RUNNER_TOKEN": "tok-25"},
    )
    envs = _flag_values(client.calls[0], "-e")
    assert "AGENTOS_RUNNER_TOKEN=tok-25" in envs


def test_runner_token_forwarded_even_when_credentials_selected() -> None:
    # Credential selection (PR #109) forwards AGENTOS_CREDENTIALS by name and
    # suppresses ambient SDK vars, but must not disturb the token, which travels
    # in the claim env as an ordinary value.
    client = _RecordingDocker(
        image="agentos-runner",
        bundle_store=_FakeBundleStore(),
        environ={"AGENTOS_CREDENTIALS": "sk-PLACEHOLDER-byo"},
    )
    client.create_claim(
        "t1",
        pool="pool",
        env={
            "AGENTOS_CREDENTIALS": "sk-PLACEHOLDER-byo",
            "AGENTOS_RUNNER_TOKEN": "tok-25b",
        },
    )
    envs = _flag_values(client.calls[0], "-e")
    assert "AGENTOS_RUNNER_TOKEN=tok-25b" in envs
    assert "AGENTOS_CREDENTIALS" in envs  # still forwarded by name
    assert "AGENTOS_CREDENTIALS=sk-PLACEHOLDER-byo" not in envs  # never as a value


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


def test_ensure_image_pulls_when_absent() -> None:
    # docker image inspect returns "" (with check=False) when the image is not
    # cached locally, so ensure_image must pull it -- the IfNotPresent + prewarm.
    client = _RecordingDocker(image="agentos-runner", bundle_store=_FakeBundleStore())
    client.outputs = {}  # "image" inspect -> "" == absent
    client.ensure_image()
    assert ["image", "inspect", "agentos-runner"] in client.calls
    assert ["pull", "agentos-runner"] in client.calls
    inspect_at = client.calls.index(["image", "inspect", "agentos-runner"])
    pull_at = client.calls.index(["pull", "agentos-runner"])
    assert inspect_at < pull_at  # inspect first, then pull


def test_ensure_image_skips_pull_when_present() -> None:
    # A non-empty inspect means the image is already cached: no pull, so an
    # offline run with the image present must not touch the network.
    client = _RecordingDocker(image="agentos-runner", bundle_store=_FakeBundleStore())
    client.outputs = {"image": '[{"Id":"sha256:cafef00d"}]'}
    client.ensure_image()
    assert ["image", "inspect", "agentos-runner"] in client.calls
    assert all(argv[0] != "pull" for argv in client.calls)


def test_ensure_image_is_best_effort_on_pull_failure(caplog) -> None:
    # A pull failure (offline, registry down) must NOT crash worker startup: a
    # truly-missing image still fails clearly later at claim time. The warning
    # names only the image -- never the argv or the docker stderr.
    class _PullFailsDocker(DockerSandboxClient):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)  # type: ignore[arg-type]
            self.calls: list[list[str]] = []

        def _docker(self, args: list[str], *, check: bool = True) -> str:
            self.calls.append(args)
            if args[0] == "pull":
                raise DockerError("docker pull failed (1): SENTINEL_STDERR_LEAK")
            return ""  # inspect: absent

    client = _PullFailsDocker(image="agentos-runner", bundle_store=_FakeBundleStore())
    with caplog.at_level(logging.WARNING, logger="agentos_worker.sandbox.docker"):
        client.ensure_image()  # must return normally, no exception propagates
    assert ["pull", "agentos-runner"] in client.calls
    text = caplog.text
    assert "agentos-runner" in text  # the warning names the image
    assert "SENTINEL_STDERR_LEAK" not in text  # but never dumps the stderr


def test_ensure_image_is_best_effort_when_docker_unavailable(caplog) -> None:
    # A missing docker binary (or unreachable daemon) makes subprocess.run raise
    # FileNotFoundError/OSError REGARDLESS of check=False, and that raise happens
    # at the inspect step -- before any pull. That OSError must NOT crash worker
    # startup: ensure_image is best-effort end to end, so the inspect call must be
    # guarded too. The warning still names only the image, never the argv.
    class _DockerUnavailable(DockerSandboxClient):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)  # type: ignore[arg-type]
            self.calls: list[list[str]] = []

        def _docker(self, args: list[str], *, check: bool = True) -> str:
            self.calls.append(args)
            if args[:2] == ["image", "inspect"]:
                # subprocess.run(["docker", ...]) raises this when the docker
                # binary is absent, independent of check=False.
                raise FileNotFoundError("docker")
            return ""

    client = _DockerUnavailable(image="agentos-runner", bundle_store=_FakeBundleStore())
    with caplog.at_level(logging.WARNING, logger="agentos_worker.sandbox.docker"):
        client.ensure_image()  # must return normally, no exception propagates
    assert ["image", "inspect", "agentos-runner"] in client.calls
    assert "agentos-runner" in caplog.text  # the warning names the image
    assert "docker inspect" not in caplog.text  # but never dumps the argv
