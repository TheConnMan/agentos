#!/bin/bash
# Scripted E2E for the agentos CLI (task I1 done-when).
#
# Round-trips a synthetic event through a real local runner container with zero
# Slack involved: init a bundle, start the runner (fake model, offline), send a
# message and stream the NDJSON reply, run the eval cases, stop. Optionally
# (AGENTOS_E2E_API_URL set) also deploys the bundle to a locally-run platform
# API.
#
# Requirements: docker, an agentos-runner image (build per runner/README.md),
# and a cargo toolchain. Run from anywhere:
#
#   bash cli/scripts/e2e.sh
#
# Env knobs:
#   AGENTOS_E2E_IMAGE     runner image (default agentos-runner)
#   AGENTOS_E2E_PORT      host port (default 7245)
#   AGENTOS_E2E_NETWORK   docker network to join (e.g. agentos_default)
#   AGENTOS_E2E_OTEL      OTLP endpoint (e.g. http://otel-collector:4318)
#   AGENTOS_E2E_API_URL   platform API base URL; enables the deploy leg
#   AGENTOS_E2E_API_KEY   platform API key (default agentos-dev-key)
set -euo pipefail

CLI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${AGENTOS_E2E_IMAGE:-agentos-runner}"
PORT="${AGENTOS_E2E_PORT:-7245}"
CONTAINER="agentos-e2e-runner"

echo "=== Build the release binary ==="
(cd "$CLI_DIR" && cargo build --release --quiet)
BIN="$CLI_DIR/target/release/agentos"
"$BIN" --version

WORKDIR="$(mktemp -d)"
cleanup() {
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    rm -rf "$WORKDIR"
}
trap cleanup EXIT

echo
echo "=== agentos init ==="
"$BIN" init deal-desk --dir "$WORKDIR/deal-desk"

cd "$WORKDIR/deal-desk"

# Eval cases matched to the runner's offline fake-model script (fake.py):
# the scripted turn streams "Looking into it", a Bash tool note, then a final
# frame whose text is "all done". The graded answer is the FINAL text only, so
# every grader below must be satisfied by "all done" (interim streamed text such
# as "looking into it" is no longer graded).
cat > evals/e2e-cases.json <<'EOF'
{
  "name": "e2e",
  "cases": [
    {
      "id": "finishes-the-turn",
      "input": "wrap it up",
      "grader": { "kind": "contains", "expected": "all done", "case_sensitive": false }
    },
    {
      "id": "reports-done",
      "input": "what is the status?",
      "grader": { "kind": "contains", "expected": "done", "case_sensitive": false }
    }
  ]
}
EOF

echo
echo "=== agentos skill up (fake model, offline) ==="
START_ARGS=(--plugin-dir . --image "$IMAGE" --port "$PORT" --name "$CONTAINER" --fake-model)
if [[ -n "${AGENTOS_E2E_NETWORK:-}" ]]; then
    START_ARGS+=(--network "$AGENTOS_E2E_NETWORK")
fi
if [[ -n "${AGENTOS_E2E_OTEL:-}" ]]; then
    START_ARGS+=(--otel-endpoint "$AGENTOS_E2E_OTEL")
fi
"$BIN" skill up "${START_ARGS[@]}"

echo
echo "=== agentos skill status ==="
"$BIN" skill status

echo
echo "=== agentos skill message (synthetic event, streamed NDJSON reply) ==="
"$BIN" skill message "@agentos can we approve the Meridian deal at 18% discount?"

echo
echo "=== agentos skill eval ==="
"$BIN" skill eval --cases evals/e2e-cases.json

if [[ -n "${AGENTOS_E2E_API_URL:-}" ]]; then
    echo
    echo "=== agentos cluster deploy (against the platform API) ==="
    "$BIN" cluster deploy --plugin-dir . \
        --api-url "$AGENTOS_E2E_API_URL" \
        --api-key "${AGENTOS_E2E_API_KEY:-agentos-dev-key}"
fi

echo
echo "=== agentos skill down ==="
"$BIN" skill down

echo
echo "E2E PASS"
