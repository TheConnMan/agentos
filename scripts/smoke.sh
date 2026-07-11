#!/usr/bin/env bash
# One-command end-to-end smoke test.
#
# Builds the runner image, then round-trips a synthetic turn through a REAL
# runner container (fake model, zero Slack, zero cluster, zero credential) via
# cli/scripts/e2e.sh: init a bundle, boot the runner, send a message, stream the
# NDJSON reply, run the eval cases, tear down. Green output = the ACI loop works.
#
# For the full dispatcher -> queue -> worker -> reply path, use the local loop
# instead (README "See it work"): agentos local up -> local deploy -> local message.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> Building the runner image (agentos-runner)..."
docker build -f runner/Dockerfile -t agentos-runner .

echo "==> Running the end-to-end round-trip (cli/scripts/e2e.sh)..."
bash cli/scripts/e2e.sh

echo "==> Smoke test passed."
