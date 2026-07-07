# Local-dev overlay for the compose `agentos-worker` service ONLY.
#
# This is NOT the hardened worker image and is NOT used by the Helm deployment.
# The hardened chart runs the worker with the KUBERNETES sandbox substrate,
# which never shells out to docker, so the published worker image ships without
# a docker CLI (dead weight and attack surface in production). The local compose
# loop instead uses the DOCKER substrate (AGENTOS_SANDBOX_SUBSTRATE=docker),
# which spawns runner containers on the host daemon via the mounted socket and
# therefore needs the docker CLI. This overlay layers just the docker client
# (not the daemon) onto the published worker image. The release pipeline
# publishes it as `agentos-worker-local` (BASE_TAG pins the base to the release
# version) so `compose.release.yaml` can run the local loop with no checkout.
ARG BASE_TAG=latest
FROM ghcr.io/curie-eng/agentos-worker:${BASE_TAG}

# Install only the static docker client binary from the official release tarball
# (the tarball also carries dockerd/containerd, which we deliberately do not
# install -- the daemon is the host's, reached through /var/run/docker.sock).
# Pinned; a client this age negotiates down to the host daemon's API version.
USER 0
ARG DOCKER_CLI_VERSION=27.5.1
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates; \
    curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz" -o /tmp/docker.tgz; \
    tar -xzf /tmp/docker.tgz -C /tmp; \
    install -m 0755 /tmp/docker/docker /usr/local/bin/docker; \
    rm -rf /tmp/docker.tgz /tmp/docker; \
    apt-get purge -y curl; \
    apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/*; \
    docker --version
