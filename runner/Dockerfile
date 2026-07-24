# Curie runner image. Productizes prototypes/runner/Dockerfile (PT-E) into the
# full ACI v0.1 runner.
#
# BUILD FROM THE REPO ROOT, not from runner/, because the runner compiles against
# the frozen workspace packages (packages/aci-protocol, packages/plugin-format):
#
#   docker build -f runner/Dockerfile -t curie-runner .
#
# The claude-agent-sdk drives the `claude` CLI, so the image carries Node plus the
# @anthropic-ai/claude-code CLI alongside the Python runner. With
# CURIE_FAKE_MODEL=1 the container answers synthetic events offline (no CLI/model
# call), which is how the container smoke round-trips without a credential.
# Python 3.13 is required by the frozen workspace packages; Node 22 is required
# by the Claude Code CLI the SDK spawns. Base on python:3.13 and add Node.
FROM python:3.13-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# The Claude Code CLI the SDK spawns for a real (non-fake) session.
RUN npm install -g @anthropic-ai/claude-code

# Off-the-shelf MCP servers pre-installed at build time so a bundle that
# declares one starts it from the on-disk binary with NO runtime network fetch
# (the runner rootfs is read-only at runtime, so an `npx`-style fetch would fail
# anyway). Bundles reference these by the package's bin name, e.g.
# `"command": "mcp-server-github"` -- see examples/github-issues. Add a line here
# to bless another authed third-party MCP server for the same shape.
RUN npm install -g @modelcontextprotocol/server-github

WORKDIR /app

# The three source trees the runner needs: the two frozen workspace packages and
# the runner itself. Copied by path so the image is reproducible from the lockfile
# above them (root pyproject/uv.lock are the workspace source of truth).
COPY packages/aci-protocol ./packages/aci-protocol
COPY packages/plugin-format ./packages/plugin-format
COPY runner ./runner

# One venv. Install the local packages first, then the runner without redeps,
# then its third-party deps. The versions are pinned == to the tested workspace
# uv.lock (source of truth: root uv.lock) so a rebuild ships exactly what CI
# exercised rather than resolving a newer SDK/aiohttp/OTel at build time. Keep
# these pins in lockstep with uv.lock when the runner's deps move. The local
# packages are installed by path first so the "aci-protocol"/"plugin-format"
# requirements resolve to the copied trees, not a same-named PyPI distribution.
RUN python3 -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir --upgrade pip \
    && /app/.venv/bin/pip install --no-cache-dir ./packages/aci-protocol ./packages/plugin-format \
    && /app/.venv/bin/pip install --no-cache-dir --no-deps ./runner \
    && /app/.venv/bin/pip install --no-cache-dir \
        "claude-agent-sdk==0.2.115" \
        "aiohttp==3.14.1" \
        "opentelemetry-sdk==1.44.0" \
        "opentelemetry-exporter-otlp-proto-http==1.44.0" \
        "anyio==4.14.1"

# Run as a non-root user. The claude-agent-sdk spawns
# `claude --dangerously-skip-permissions` (permission_mode=bypassPermissions),
# and Claude Code refuses that flag under uid 0 ("cannot be used with root/sudo
# privileges"), so a root image fails every real-model boot. uid:gid 1000:1000
# matches the A2 chart hardening (runAsUser/runAsGroup/fsGroup 1000) and HOME is
# /home/runner (one of the chart's writablePaths). The venv and the global npm
# `claude` CLI live in root-owned, world-readable paths, so uid 1000 imports the
# deps and executes the CLI without owning them. HOME is the only writable path
# the runtime needs; under the A2 read-only rootfs the chart mounts writable
# emptyDirs at /tmp and /home/runner, and nothing here writes elsewhere.
RUN groupadd --gid 1000 runner \
    && useradd --uid 1000 --gid 1000 --home-dir /home/runner --create-home --shell /usr/sbin/nologin runner \
    && chown -R 1000:1000 /home/runner
ENV HOME=/home/runner

ENV PATH="/app/.venv/bin:$PATH"
ENV CURIE_RUNNER_PORT=8080
EXPOSE 8080

USER 1000:1000
CMD ["python", "-m", "curie_runner"]
