# AgentOS runner image. Productizes prototypes/runner/Dockerfile (PT-E) into the
# full ACI v0.1 runner.
#
# BUILD FROM THE REPO ROOT, not from runner/, because the runner compiles against
# the frozen workspace packages (packages/aci-protocol, packages/plugin-format):
#
#   docker build -f runner/Dockerfile -t agentos-runner .
#
# The claude-agent-sdk drives the `claude` CLI, so the image carries Node plus the
# @anthropic-ai/claude-code CLI alongside the Python runner. With
# AGENTOS_FAKE_MODEL=1 the container answers synthetic events offline (no CLI/model
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

WORKDIR /app

# The three source trees the runner needs: the two frozen workspace packages and
# the runner itself. Copied by path so the image is reproducible from the lockfile
# above them (root pyproject/uv.lock are the workspace source of truth).
COPY packages/aci-protocol ./packages/aci-protocol
COPY packages/plugin-format ./packages/plugin-format
COPY runner ./runner

# One venv, deps pinned to the runner's pyproject. Install the local packages
# first, then the runner without redeps, then its third-party deps explicitly, so
# the local "aci-protocol"/"plugin-format" requirements resolve to the copied
# trees rather than any same-named PyPI distribution.
RUN python3 -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir --upgrade pip \
    && /app/.venv/bin/pip install --no-cache-dir ./packages/aci-protocol ./packages/plugin-format \
    && /app/.venv/bin/pip install --no-cache-dir --no-deps ./runner \
    && /app/.venv/bin/pip install --no-cache-dir \
        "claude-agent-sdk>=0.2.110" \
        "aiohttp>=3.11" \
        "opentelemetry-sdk>=1.30" \
        "opentelemetry-exporter-otlp-proto-http>=1.30" \
        "anyio>=4.6"

ENV PATH="/app/.venv/bin:$PATH"
ENV AGENTOS_RUNNER_PORT=8080
EXPOSE 8080

CMD ["python", "-m", "agentos_runner"]
