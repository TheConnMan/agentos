#!/bin/bash
# Falsifiability gate, real-path negative control (issue #619).
#
# This is a FALSIFIABILITY gate, NOT an E2E test: it never runs a real agent or
# makes a model call. It boots the runner's scripted FAKE model (offline, no
# credential, no network) -- a do-nothing agent whose only reply is the canned
# final "all done" -- and runs every COMMITTED eval suite through the real
# `agentos skill eval` path. A genuinely falsifiable case (#527) must go RED
# against this do-nothing agent, so the gate FAILS if ANY committed case passes.
#
# Suites are discovered, not hardcoded: every examples/*/evals/cases.json plus
# the `agentos init` scaffold seed at apps/worker/schema/eval-cases.example.json.
# Adding a suite needs no edit here.
#
# The input-parrot vacuousness control (AC4) and the positive control (AC2) are
# grader-level and live in cli/tests/eval_falsifiability.rs -- the fake model can
# only ever say "all done", so those exemplars cannot be expressed through this
# real path. Together the two halves are the gate.
#
# Requirements: docker + an agentos-runner image (built by CI, or `agentos build`
# locally). The CLI binary is reused from $AGENTOS_BIN if set+executable, else
# built with cargo. Run from anywhere:
#
#   bash cli/scripts/eval-falsifiability.sh
#
# Env knobs:
#   AGENTOS_BIN                path to a prebuilt agentos binary (skip cargo build)
#   AGENTOS_FALSIFY_IMAGE      runner image (default agentos-runner)
#   AGENTOS_FALSIFY_PORT       host port (default 7246)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="${AGENTOS_FALSIFY_IMAGE:-agentos-runner}"
PORT="${AGENTOS_FALSIFY_PORT:-7246}"
CONTAINER="agentos-falsifiability-runner"
BUNDLE="$REPO_ROOT/examples/weather"

echo "=== Resolve the agentos binary ==="
if [[ -n "${AGENTOS_BIN:-}" && -x "${AGENTOS_BIN:-}" ]]; then
    # Absolutize: the gate cd's into a throwaway dir before invoking the binary,
    # so a relative $AGENTOS_BIN (as CI passes) must be pinned to an absolute path
    # here or it stops resolving after the cd.
    BIN="$(cd "$(dirname "$AGENTOS_BIN")" && pwd)/$(basename "$AGENTOS_BIN")"
    echo "using prebuilt binary: $BIN"
else
    (cd "$REPO_ROOT/cli" && cargo build --release --quiet)
    BIN="$REPO_ROOT/cli/target/release/agentos"
fi
"$BIN" --version

# Discover every committed suite: examples/*/evals/cases.json + the scaffold seed.
mapfile -t SUITES < <(find "$REPO_ROOT/examples" -mindepth 2 -maxdepth 3 -path '*/evals/cases.json' | sort)
SUITES+=("$REPO_ROOT/apps/worker/schema/eval-cases.example.json")
echo
echo "=== Committed suites under gate (${#SUITES[@]}) ==="
printf '  %s\n' "${SUITES[@]#"$REPO_ROOT"/}"
if (( ${#SUITES[@]} < 3 )); then
    echo "expected at least 3 committed suites; found ${#SUITES[@]}" >&2
    exit 1
fi

WORKDIR="$(mktemp -d)"
cleanup() {
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    rm -rf "$WORKDIR"
}
trap cleanup EXIT

# Mount a throwaway COPY of a valid bundle so the recorded .agentos/runner.json
# lands in the temp dir, never in the tree. The fake model ignores the bundle
# and the input entirely (it only ever replies "all done"), so any valid bundle
# serves; the eval below dials the runner by explicit --url regardless.
cp -r "$BUNDLE" "$WORKDIR/bundle"

echo
echo "=== agentos skill up (fake model, offline) ==="
"$BIN" skill up \
    --plugin-dir "$WORKDIR/bundle" \
    --image "$IMAGE" \
    --port "$PORT" \
    --name "$CONTAINER" \
    --fake-model
URL="http://localhost:$PORT"

echo
echo "=== Negative control: every committed case must go RED against the fake model ==="
FAILED=0
for suite in "${SUITES[@]}"; do
    rel="${suite#"$REPO_ROOT"/}"
    echo "--- $rel"
    # skill eval exits non-zero when ANY case is RED (here that is ALL of them,
    # which is the pass condition, not a failure); capture the JSON regardless of
    # exit code and assert passed==0 off the parsed rollup, not off the exit code.
    out="$("$BIN" --json skill eval --cases "$suite" --url "$URL" || true)"
    passed="$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin)["passed"])' 2>/dev/null || echo "ERR")"
    total="$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin)["total"])' 2>/dev/null || echo "ERR")"
    if [[ "$passed" == "ERR" || "$total" == "ERR" ]]; then
        echo "    could not parse eval --json output:" >&2
        printf '%s\n' "$out" >&2
        FAILED=1
        continue
    fi
    echo "    $passed/$total passed against the do-nothing fake agent"
    if [[ "$passed" != "0" ]]; then
        greeners="$(printf '%s' "$out" | python3 -c 'import json,sys; print(", ".join(c["id"] for c in json.load(sys.stdin)["cases"] if c["passed"]))' 2>/dev/null || echo "(unparseable)")"
        echo "    UNFALSIFIABLE: these cases pass against a do-nothing agent (#527): $greeners" >&2
        FAILED=1
    fi
done

echo
echo "=== agentos skill down ==="
( cd "$WORKDIR/bundle" && "$BIN" skill down ) || true

if (( FAILED )); then
    echo
    echo "FALSIFIABILITY GATE FAILED: at least one committed eval case greens against the fake model." >&2
    exit 1
fi

echo
echo "FALSIFIABILITY GATE PASS: every committed eval case is red against the fake model."
