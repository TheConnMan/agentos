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
echo "=== agentos init --from-spec (non-interactive, agent-authored spec) ==="
# AC #2: a coding agent writes a spec, the CLI scaffolds a runnable bundle from
# it with zero prompts, and the spec-scaffolded evals/cases.json runs on the eval
# path. The grader is falsifiable (it requires the agent to name itself), NOT
# tuned to the fake model's canned "all done" reply: a grader written to match
# the fake manufactures a green, and CI carrying one is exactly why #612 went
# unnoticed. Under --fake-model the grader is never consulted at all -- the run
# reports the non-graded plumbing_ok (ADR-0055).
cat > "$WORKDIR/agent-spec.json" <<'EOF'
{
  "name": "deal-desk",
  "description": "Prices and reviews deal desk requests.",
  "skills": [
    {
      "name": "deal-desk",
      "description": "Invoke when a rep submits a pricing exception request.",
      "allowed_tools": ["WebSearch", "WebFetch"],
      "instructions": "Price the exception against the guardrails, then summarize the decision.\n"
    }
  ],
  "evals": [
    {
      "id": "introduces-itself",
      "input": "In one short sentence, introduce yourself as the deal-desk agent.",
      "grader": { "kind": "contains", "expected": "deal-desk", "case_sensitive": false }
    }
  ]
}
EOF
"$BIN" init --from-spec "$WORKDIR/agent-spec.json" --dir "$WORKDIR/deal-desk"

cd "$WORKDIR/deal-desk"

# A second suite for the explicit `--cases` leg. Its graders are real domain
# graders, deliberately NOT matched to the fake-model script's canned "all done"
# reply: this run is offline under --fake-model, so the graders are never
# consulted and the suite reports plumbing_ok. Writing them to match the fake
# would only re-create the bypass that let #612 ship green.
cat > evals/e2e-cases.json <<'EOF'
{
  "name": "e2e",
  "cases": [
    {
      "id": "introduces-itself",
      "input": "In one short sentence, introduce yourself as the deal-desk agent.",
      "grader": { "kind": "contains", "expected": "deal-desk", "case_sensitive": false }
    },
    {
      "id": "names-its-domain",
      "input": "What kind of requests do you handle?",
      "grader": { "kind": "contains", "expected": "pricing", "case_sensitive": false }
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
echo "=== agentos skill eval (spec-scaffolded evals/cases.json) ==="
# No --cases: exercise the evals/cases.json the --from-spec scaffold wrote,
# proving spec -> bundle -> skill eval passes end to end offline (AC #2).
"$BIN" skill eval

echo
echo "=== agentos skill eval (explicit cases file) ==="
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
