# Local-dev overlay for the compose `curie-worker` service ONLY.
#
# This is NOT the hardened worker image and is NOT used by the Helm deployment.
# The hardened chart runs the worker with the KUBERNETES sandbox substrate,
# which never shells out to docker, so the published worker image ships without
# a docker CLI (dead weight and attack surface in production). The local compose
# loop instead uses the DOCKER substrate (CURIE_SANDBOX_SUBSTRATE=docker),
# which spawns runner containers on the host daemon via the mounted socket and
# therefore needs the docker CLI. This overlay layers just the docker client
# (not the daemon) onto the published worker image. The release pipeline
# publishes it as `curie-worker-local` (BASE_TAG pins the base to the release
# version) so `compose.release.yaml` can run the local loop with no checkout.
ARG BASE_TAG=latest
FROM ghcr.io/curie-eng/curie-worker:${BASE_TAG}

# Install only the static docker client binary from the official release tarball
# (the tarball also carries dockerd/containerd, which we deliberately do not
# install -- the daemon is the host's, reached through /var/run/docker.sock).
# Pinned; a client this age negotiates down to the host daemon's API version.
USER 0
ARG DOCKER_CLI_VERSION=27.5.1
# TARGETARCH is set automatically by buildx per target platform (amd64/arm64).
# Docker publishes the static CLI tarball under x86_64/ and aarch64/ dirs, so map
# the Go-style arch name to the download arch instead of hardcoding x86_64.
ARG TARGETARCH
RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) DOCKER_ARCH=x86_64 ;; \
      arm64) DOCKER_ARCH=aarch64 ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates; \
    curl -fsSL "https://download.docker.com/linux/static/stable/${DOCKER_ARCH}/docker-${DOCKER_CLI_VERSION}.tgz" -o /tmp/docker.tgz; \
    tar -xzf /tmp/docker.tgz -C /tmp; \
    install -m 0755 /tmp/docker/docker /usr/local/bin/docker; \
    rm -rf /tmp/docker.tgz /tmp/docker; \
    apt-get purge -y curl; \
    apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/*; \
    docker --version
