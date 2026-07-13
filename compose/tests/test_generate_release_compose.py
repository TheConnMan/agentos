"""Contract tests for the compose release generator (compose/generate_release_compose.py).

The generator turns compose.dev.yaml into a self-contained release compose file via
three text transforms: (1) replace the agentos-worker build overlay with a pinned
worker-local image, (2) inline otel/collector-config.yaml as a top-level `configs:`
block (re-indented 6 spaces, `${env:` escaped to `$${env:`) and repoint the
otel-collector service at it, and (3) pin every ghcr agentos-* image tag to the
release version. These tests assert on those transforms and on invariants preserved
from the dev stack. They deliberately do NOT compare byte-for-byte against the
hand-maintained compose.release.yaml, which has drifted from dev.
"""

import importlib.util
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "compose" / "generate_release_compose.py"
DEV_PATH = REPO_ROOT / "compose.dev.yaml"
OTEL_PATH = REPO_ROOT / "otel" / "collector-config.yaml"

DEV_TEXT = DEV_PATH.read_text()
OTEL_TEXT = OTEL_PATH.read_text()

AGENTOS_LATEST_RE = re.compile(r"ghcr\.io/curie-eng/agentos-[a-z-]+:latest")
AGENTOS_IMAGE_RE = re.compile(r"ghcr\.io/curie-eng/agentos-[a-z-]+:(\S+)")
# `${env:` not preceded by a `$` -> an UNescaped collector-config reference.
UNESCAPED_ENV_RE = re.compile(r"(?<!\$)\$\{env:")


def load_generate():
    """Import the standalone generator script by path (compose/ is not on sys.path)."""
    spec = importlib.util.spec_from_file_location("generate_release_compose", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.generate


def run_cli(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def service_names(text):
    """Extract the set of service keys (2-space-indented `  name:` under `services:`)."""
    names = set()
    in_services = False
    for line in text.splitlines():
        if re.match(r"^services:\s*$", line):
            in_services = True
            continue
        if in_services and re.match(r"^\S", line):
            break  # next top-level key (e.g. `volumes:`)
        if in_services:
            m = re.match(r"^ {2}([A-Za-z0-9_-]+):\s*$", line)
            if m:
                names.add(m.group(1))
    return names


def service_block(text, name):
    """Return the text of a single service block, header through last body line."""
    out = []
    capturing = False
    for line in text.splitlines():
        if re.match(rf"^ {{2}}{re.escape(name)}:\s*$", line):
            capturing = True
            out.append(line)
            continue
        if capturing:
            # A new 2-space-indented header/comment or a top-level key ends the block.
            if re.match(r"^ {2}\S", line) or re.match(r"^\S", line):
                break
            out.append(line)
    return "\n".join(out)


def test_worker_build_overlay_becomes_pinned_image():
    generate = load_generate()
    out = generate(DEV_TEXT, OTEL_TEXT, version="9.9.9")

    worker = service_block(out, "agentos-worker")
    assert worker, "agentos-worker service block not found in generated output"
    assert "image: ghcr.io/curie-eng/agentos-worker-local:9.9.9" in worker
    assert "build:" not in worker
    assert "worker-local.Dockerfile" not in worker
    assert "worker-local.Dockerfile" not in out


def test_otel_config_is_inlined_and_escaped():
    generate = load_generate()
    out = generate(DEV_TEXT, OTEL_TEXT, version="9.9.9")

    # A new top-level configs block holds the collector config as a literal scalar.
    assert re.search(r"^configs:\s*$", out, re.MULTILINE)
    assert "otel_collector_config:" in out
    assert "content: |" in out

    # The inlined content is the collector config re-indented 6 spaces with the
    # `${env:` interpolation escaped to `$${env:` (compose interpolation escape).
    expected_block = textwrap.indent(OTEL_TEXT.replace("${env:", "$${env:"), "      ")
    assert expected_block in out

    # The escaped auth line is present, and NO unescaped `${env:` remains anywhere.
    assert "$${env:LANGFUSE_OTLP_AUTH_HEADER}" in out
    assert UNESCAPED_ENV_RE.search(out) is None


def test_otel_collector_references_config_not_host_mount():
    generate = load_generate()
    out = generate(DEV_TEXT, OTEL_TEXT, version="9.9.9")

    collector = service_block(out, "otel-collector")
    assert collector, "otel-collector service block not found in generated output"
    assert "source: otel_collector_config" in collector
    assert "target: /etc/otel/collector-config.yaml" in collector
    # The host bind-mount of the config file is gone.
    assert "./otel/collector-config.yaml" not in out


def test_agentos_images_pinned_non_agentos_untouched():
    generate = load_generate()
    out = generate(DEV_TEXT, OTEL_TEXT, version="9.9.9")

    # Every agentos-* image is pinned to the release version; none left at :latest.
    assert AGENTOS_LATEST_RE.search(out) is None
    tags = AGENTOS_IMAGE_RE.findall(out)
    assert tags, "expected at least one ghcr agentos-* image in the output"
    assert all(tag == "9.9.9" for tag in tags)

    # Non-agentos images are never rewritten.
    assert "image: postgres:16-alpine" in out
    assert "image: otel/opentelemetry-collector-contrib:0.119.0" in out


def test_invariants_preserved_from_dev():
    generate = load_generate()
    out = generate(DEV_TEXT, OTEL_TEXT, version="9.9.9")

    assert "x-core-profiles: &core_profiles [core, full]" in out
    assert "x-full-profiles: &full_profiles [full]" in out
    assert out.count("profiles: *core_profiles") == 7
    assert out.count("profiles: *full_profiles") == 5

    # No service is added or dropped by the transforms.
    assert service_names(out) == service_names(DEV_TEXT)


def test_default_version_latest_leaves_latest_tags():
    generate = load_generate()
    out = generate(DEV_TEXT, OTEL_TEXT, version="latest")

    worker = service_block(out, "agentos-worker")
    assert "image: ghcr.io/curie-eng/agentos-worker-local:latest" in worker
    assert "build:" not in worker

    tags = AGENTOS_IMAGE_RE.findall(out)
    assert tags
    assert all(tag == "latest" for tag in tags)


def test_cli_prints_generated_yaml():
    result = run_cli("--version", "9.9.9")
    assert result.returncode == 0, result.stderr
    out = result.stdout

    assert "image: ghcr.io/curie-eng/agentos-worker-local:9.9.9" in out
    assert AGENTOS_LATEST_RE.search(out) is None
    assert re.search(r"^configs:\s*$", out, re.MULTILINE)
    assert "$${env:LANGFUSE_OTLP_AUTH_HEADER}" in out
    assert service_names(out) == service_names(DEV_TEXT)


def test_cli_default_version_is_latest():
    result = run_cli()
    assert result.returncode == 0, result.stderr
    out = result.stdout

    assert "image: ghcr.io/curie-eng/agentos-worker-local:latest" in out
    tags = AGENTOS_IMAGE_RE.findall(out)
    assert tags
    assert all(tag == "latest" for tag in tags)


def assert_init_containers_adopted(compose_text, label):
    """Assert every one-shot init container is adopted via
    `service_completed_successfully` in every profile combo `agentos local up`
    can activate.

    `docker compose up --wait` treats a one-shot init container's clean exit(0)
    as a failure unless some service in the up-set depends on it with
    `condition: service_completed_successfully`. `agentos local up` activates a
    base profile (core or full), optionally + `local-model`, optionally +
    `slack` -> 8 combos. Every one-shot init started in a combo must be adopted
    by a long-running service that is itself started in that same combo.
    """
    doc = yaml.safe_load(compose_text)
    services = doc["services"]

    combos = []
    for base in ({"core"}, {"full"}):
        for with_model in (False, True):
            for with_slack in (False, True):
                combo = set(base)
                if with_model:
                    combo.add("local-model")
                if with_slack:
                    combo.add("slack")
                combos.append(frozenset(combo))

    def is_started(spec, combo):
        profiles = spec.get("profiles")
        if not profiles:
            return True
        return bool(set(profiles) & combo)

    def is_oneshot(spec):
        return spec.get("restart") == "no"

    def adopts(spec, init):
        """True if this (long-running) service depends on `init` with
        condition service_completed_successfully."""
        depends = spec.get("depends_on")
        if not isinstance(depends, dict):
            # list form carries no condition, or depends_on absent -> no adoption
            return False
        entry = depends.get(init)
        return (
            isinstance(entry, dict) and entry.get("condition") == "service_completed_successfully"
        )

    violations = []
    for combo in combos:
        started = {name: spec for name, spec in services.items() if is_started(spec, combo)}
        for init, init_spec in started.items():
            if not is_oneshot(init_spec):
                continue
            adopted = any(
                other != init and not is_oneshot(other_spec) and adopts(other_spec, init)
                for other, other_spec in started.items()
            )
            if not adopted:
                violations.append((sorted(combo), init))

    assert not violations, (
        f"{label}: one-shot init container(s) unadopted by any "
        f"service_completed_successfully dependency in an activatable profile "
        f"combo: "
        + "; ".join(
            f"init '{init}' unadopted in profiles {profiles}" for profiles, init in violations
        )
    )


def test_dev_compose_init_containers_adopted():
    assert_init_containers_adopted(DEV_TEXT, "compose.dev.yaml")


def test_release_compose_init_containers_adopted():
    generate = load_generate()
    release_text = generate(DEV_TEXT, OTEL_TEXT, version="1.2.3")
    assert_init_containers_adopted(release_text, "compose.release.yaml")


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not available")
def test_generated_compose_validates_with_docker(tmp_path):
    generate = load_generate()
    out = generate(DEV_TEXT, OTEL_TEXT, version="latest")

    compose_file = tmp_path / "compose.release.yaml"
    compose_file.write_text(out)

    result = subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "config", "-q"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
