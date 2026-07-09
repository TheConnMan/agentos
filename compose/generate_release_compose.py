#!/usr/bin/env python3
"""Generate the self-contained release compose file from compose.dev.yaml.

compose.dev.yaml is the single source of truth for the local stack. The release
asset (`compose.release.yaml`, shipped to `agentos local up` on a release binary)
must not depend on a repo checkout, so this script derives it from the dev file
via three ordered text transforms:

  T1  Replace the agentos-worker build overlay (`build: {context, dockerfile}`)
      with a pinned `image: ghcr.io/curie-eng/agentos-worker-local:latest`, since
      the release stack cannot build the worker-local overlay from source.

  T2  Inline otel/collector-config.yaml as a top-level `configs:` block (a literal
      scalar, re-indented 6 spaces, with `${env:` escaped to `$${env:` so compose
      does not try to interpolate the collector's own env references), and repoint
      the otel-collector service from the host bind-mount to that config.

  T3  Pin every `ghcr.io/curie-eng/agentos-*:latest` image tag to the release
      version (this also pins the worker-local image introduced by T1).

Each transform locates its anchor explicitly and raises ValueError if it is
missing: this runs unattended at publish time, so a silent no-op would ship a
broken release asset. Fail loud instead.
"""

import argparse
import re
import textwrap
from pathlib import Path

WORKER_BUILD_BLOCK = """    build:
      context: compose
      dockerfile: worker-local.Dockerfile
"""
WORKER_IMAGE_LINE = "    image: ghcr.io/curie-eng/agentos-worker-local:latest\n"

CONFIGS_ANCHOR = "x-core-profiles: &core_profiles [core, full]"

OTEL_VOLUME_BLOCK = """    volumes:
      - ./otel/collector-config.yaml:/etc/otel/collector-config.yaml:ro
"""
OTEL_CONFIGS_REF = """    configs:
      - source: otel_collector_config
        target: /etc/otel/collector-config.yaml
"""

AGENTOS_LATEST_RE = re.compile(r"(ghcr\.io/curie-eng/agentos-[a-z-]+):latest")

DEV_COMPOSE = Path("compose.dev.yaml")
OTEL_CONFIG = Path("otel/collector-config.yaml")


def generate(dev_text: str, otel_text: str, version: str) -> str:
    """Apply transforms T1, T2, T3 in order and return the release compose text."""
    text = dev_text

    # T1: worker build overlay -> pinned worker-local image.
    if WORKER_BUILD_BLOCK not in text:
        raise ValueError(
            "T1: agentos-worker build overlay block not found in compose.dev.yaml"
        )
    text = text.replace(WORKER_BUILD_BLOCK, WORKER_IMAGE_LINE, 1)

    # T2a: inline the collector config as a top-level configs block.
    if CONFIGS_ANCHOR not in text:
        raise ValueError(f"T2: anchor line not found: {CONFIGS_ANCHOR!r}")
    body = textwrap.indent(otel_text.replace("${env:", "$${env:"), "      ")
    configs_block = "configs:\n  otel_collector_config:\n    content: |\n" + body
    text = text.replace(CONFIGS_ANCHOR, configs_block + "\n" + CONFIGS_ANCHOR, 1)

    # T2b: repoint otel-collector from the host bind-mount to the inlined config.
    if OTEL_VOLUME_BLOCK not in text:
        raise ValueError(
            "T2: otel-collector host bind-mount block not found in compose.dev.yaml"
        )
    text = text.replace(OTEL_VOLUME_BLOCK, OTEL_CONFIGS_REF, 1)

    # T3: pin every agentos-* image tag to the release version (worker-local too).
    text = AGENTOS_LATEST_RE.sub(rf"\1:{version}", text)

    return text


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate compose.release.yaml from compose.dev.yaml."
    )
    parser.add_argument("--version", default="latest", help="release version to pin image tags to")
    args = parser.parse_args()

    dev_text = DEV_COMPOSE.read_text()
    otel_text = OTEL_CONFIG.read_text()
    result = generate(dev_text, otel_text, args.version)
    print(result, end="")


if __name__ == "__main__":
    main()
