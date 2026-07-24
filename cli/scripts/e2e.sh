#!/bin/bash
# Scripted E2E for the curie CLI (task I1 done-when).
#
# Round-trips a synthetic event through a real local runner container with zero
# Slack involved: init a bundle, start the runner (fake model, offline by
# default; live model under CURIE_E2E_LIVE=1), send a message and stream the
# NDJSON reply, run the eval cases, stop. This is rung 1 (skill) of the
# cold-start parity ladder (issue #690, cli/scripts/e2e-ladder.sh); the
# ladder's local rung (`local deploy` -> `local message` with a real reply
# assertion) covers deploying a bundle against a running platform API, so this
# script no longer does so itself (issue #694).
#
# Requirements: docker, an curie-runner image (build per runner/README.md),
# and a cargo toolchain (or $CURIE_BIN). Run from anywhere:
#
#   bash cli/scripts/e2e.sh
#
# Env knobs:
#   CURIE_E2E_IMAGE     runner image (default curie-runner)
#   CURIE_E2E_PORT      host port (default 7245)
#   CURIE_E2E_NETWORK   docker network to join (e.g. curie_default)
#   CURIE_E2E_OTEL      OTLP endpoint (e.g. http://otel-collector:4318)
#   CURIE_E2E_LIVE      1 = real model, requiring a credential in the
#                         environment (ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN,
#                         or CURIE_CREDENTIALS); default 0 runs the runner's
#                         scripted fake model, offline and credential-free. This
#                         is the SAME env var cli/scripts/e2e-ladder.sh sets for
#                         its own local and cluster rungs, so a single
#                         CURIE_E2E_LIVE=1 now runs every rung live.
#   CURIE_BIN           path to a prebuilt curie binary (skip cargo build)
set -euo pipefail

CLI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${CURIE_E2E_IMAGE:-curie-runner}"
PORT="${CURIE_E2E_PORT:-7245}"
CONTAINER="curie-e2e-runner"
LIVE="${CURIE_E2E_LIVE:-0}"

echo "=== Resolve the curie binary ==="
if [[ -n "${CURIE_BIN:-}" && -x "${CURIE_BIN:-}" ]]; then
    # Absolutize: this script cd's into a scaffolded bundle directory before
    # invoking the binary, so a relative $CURIE_BIN (as the ladder and CI
    # pass) must be pinned to an absolute path here or it stops resolving
    # after the cd.
    BIN="$(cd "$(dirname "$CURIE_BIN")" && pwd)/$(basename "$CURIE_BIN")"
    echo "using prebuilt binary: $BIN"
else
    (cd "$CLI_DIR" && cargo build --release --quiet)
    BIN="$CLI_DIR/target/release/curie"
fi
"$BIN" --version

echo
echo "=== Resolve model mode ==="
if [[ "$LIVE" == "1" ]]; then
    if [[ -z "${ANTHROPIC_API_KEY:-}" && -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" && -z "${CURIE_CREDENTIALS:-}" ]]; then
        echo "error: CURIE_E2E_LIVE=1 needs a model credential in the environment, and none is set." >&2
        echo "fix: export ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN, or CURIE_CREDENTIALS, or drop CURIE_E2E_LIVE to run sealed against the fake model." >&2
        exit 1
    fi
    echo "model mode: LIVE (real model; \`skill up\` forwards the ambient credential)"
else
    echo "model mode: FAKE (sealed; --fake-model, offline, no credential)"
fi

WORKDIR="$(mktemp -d)"
cleanup() {
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    rm -rf "$WORKDIR"
}
trap cleanup EXIT

echo
echo "=== curie init --from-spec (non-interactive, agent-authored spec) ==="
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
if [[ "$LIVE" == "1" ]]; then
    echo "=== curie skill up (live model) ==="
else
    echo "=== curie skill up (fake model, offline) ==="
fi
START_ARGS=(--plugin-dir . --image "$IMAGE" --port "$PORT" --name "$CONTAINER")
if [[ "$LIVE" == "1" ]]; then
    : # Real credential resolution (CURIE_CREDENTIALS, else the ambient SDK
      # creds) happens inside `skill up` itself once --fake-model is omitted;
      # see commands::select_passthrough_env.
else
    START_ARGS+=(--fake-model)
fi
if [[ -n "${CURIE_E2E_NETWORK:-}" ]]; then
    START_ARGS+=(--network "$CURIE_E2E_NETWORK")
fi
if [[ -n "${CURIE_E2E_OTEL:-}" ]]; then
    START_ARGS+=(--otel-endpoint "$CURIE_E2E_OTEL")
fi
"$BIN" skill up "${START_ARGS[@]}"

echo
echo "=== curie skill status ==="
"$BIN" skill status

echo
echo "=== curie skill message (synthetic event, streamed NDJSON reply) ==="
"$BIN" skill message "@curie can we approve the Meridian deal at 18% discount?"

echo
echo "=== curie skill eval (spec-scaffolded evals/cases.json) ==="
# No --cases: exercise the evals/cases.json the --from-spec scaffold wrote,
# proving spec -> bundle -> skill eval passes end to end offline (AC #2).
"$BIN" skill eval

echo
echo "=== curie skill eval (explicit cases file) ==="
"$BIN" skill eval --cases evals/e2e-cases.json

echo
echo "=== curie skill down ==="
"$BIN" skill down

echo
echo "E2E PASS"
