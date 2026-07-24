#!/usr/bin/env bash
#
# Render-assertion test for the dispatcher's platform-API wiring (#442). Proves,
# with `helm template` alone (no cluster), that the dispatcher Deployment is told
# where the API is and how to authenticate to it. Unwired, the dispatcher falls
# back to its code default http://localhost:8000, which inside its own pod is the
# dispatcher itself, and every Slack Approve click dead-ends with only a warning.
#
#   1. Default install renders CURIE_API_URL as the in-chart API Service
#      (http://<fullname>-api:<api.service.port>), asserted as a VALUE.
#   2. The port tracks .Values.api.service.port and is not hardcoded.
#   3. dispatcher.apiBaseUrl overrides verbatim (the BYO / api.deploy=false path).
#   4. CURIE_API_KEY arrives by secretKeyRef to the chart Secret's `apiKey`
#      key, never as an inline literal (which would land the credential in
#      `helm get manifest` and in any rendered artifact CI uploads).
#   5. A token-less install still renders no dispatcher at all (unchanged gate).
#
# NOTE ON `--output-dir`: the sibling scripts in this directory capture
# `helm template` through command substitution. Do NOT copy that here, and do not
# "fix" this script back to a pipe. In this environment `helm template` into a
# stdout pipe has been observed to truncate silently at ~41 lines while still
# exiting 0, which turns a rendered-fine env var into a reported-absent FALSE
# NEGATIVE (and could equally report one present by luck). Rendering to a
# directory and reading the written file is the only trustworthy form here.
#
# Fails loudly, naming the assertion. Runnable locally and from CI.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART="$(cd "$SCRIPT_DIR/.." && pwd)"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail() {
  echo "ASSERTION FAILED: $1" >&2
  exit 1
}

# The dispatcher template is gated on both Slack tokens being set
# (curie.dispatcher.enabled), so every render that expects a dispatcher must
# supply them.
TOKENS=(
  --set dispatcher.slack.appToken=xapp-assert
  --set dispatcher.slack.botToken=xoxb-assert
)

# Render to a directory and echo the path of the dispatcher Deployment manifest.
render_dispatcher() {
  local name="$1"
  shift
  local out="$TMP/$name"
  mkdir -p "$out"
  helm template curie "$CHART" --output-dir "$out" "$@" >/dev/null
  local manifest="$out/curie/templates/dispatcher.yaml"
  [ -f "$manifest" ] || fail "$name: dispatcher.yaml did not render at all"
  echo "$manifest"
}

# Read one env entry out of the rendered dispatcher container structurally, via
# PyYAML -- the same convention as read_key() in render-assertions.sh and the
# ASSERT_PY pass in controller-rbac-assertions.sh. Deliberately NOT grep/awk over
# the text: a line-oriented state machine silently mis-reads a requoted value, a
# reordered key, or a `valueFrom` that renders before `value`, and it fails as a
# FALSE PASS -- exactly the class this script's header warns about.
#
#   env_value  <manifest> <name>   -> the entry's `value`, empty if absent
#   env_field  <manifest> <name> <dotted-path>
#                                  -> a nested field (e.g. valueFrom.secretKeyRef.key)
#   env_has    <manifest> <name> <dotted-path>
#                                  -> exit 0 if the path exists, 1 if not
ENV_PY="$TMP/env.py"
cat > "$ENV_PY" <<'PY'
import sys, yaml

manifest, name = sys.argv[1], sys.argv[2]
path = sys.argv[3] if len(sys.argv) > 3 else "value"

with open(manifest) as f:
    docs = [d for d in yaml.safe_load_all(f) if d]

entries = [
    e
    for d in docs
    if d.get("kind") == "Deployment"
    for c in ((d.get("spec") or {}).get("template") or {}).get("spec", {}).get("containers") or []
    for e in (c.get("env") or [])
    if e.get("name") == name
]
if not entries:
    sys.exit(1)
if len(entries) > 1:
    sys.stderr.write("env %r appears %d times in the dispatcher\n" % (name, len(entries)))
    sys.exit(2)

node = entries[0]
for part in path.split("."):
    if not isinstance(node, dict) or part not in node:
        sys.exit(1)
    node = node[part]
sys.stdout.write(str(node))
PY

env_value() { python3 "$ENV_PY" "$1" "$2" || true; }
env_field() { python3 "$ENV_PY" "$1" "$2" "$3" || true; }
env_has() { python3 "$ENV_PY" "$1" "$2" "$3" >/dev/null; }

# 1: default install renders the in-chart API Service name and port as a value.
# This render is reused by assertion 4 below (identical arguments), so it is
# deliberately kept in its own variable rather than re-rendered.
default_manifest="$(render_dispatcher default "${TOKENS[@]}")"
actual="$(env_value "$default_manifest" CURIE_API_URL)"
[ -n "$actual" ] \
  || fail "default install: dispatcher has no CURIE_API_URL env value; it will fall back to http://localhost:8000 (itself) and Slack approval clicks will dead-end"
[ "$actual" = "http://curie-api:8000" ] \
  || fail "default install: CURIE_API_URL is '$actual', expected 'http://curie-api:8000' (the in-chart API Service)"

# 2: the port comes from .Values.api.service.port, not a hardcoded 8000.
manifest="$(render_dispatcher port --set api.service.port=9999 "${TOKENS[@]}")"
actual="$(env_value "$manifest" CURIE_API_URL)"
[ "$actual" = "http://curie-api:9999" ] \
  || fail "api.service.port=9999: CURIE_API_URL is '$actual', expected 'http://curie-api:9999' (the port is hardcoded in the template instead of read from .Values.api.service.port)"

# 3: BYO override renders verbatim (the api.deploy=false answer).
manifest="$(render_dispatcher byo --set dispatcher.apiBaseUrl=http://byo-api.example:8080 "${TOKENS[@]}")"
actual="$(env_value "$manifest" CURIE_API_URL)"
[ "$actual" = "http://byo-api.example:8080" ] \
  || fail "dispatcher.apiBaseUrl override: CURIE_API_URL is '$actual', expected the verbatim override 'http://byo-api.example:8080'"

# 4: the API key arrives by reference to the chart Secret, never inline. Reuses
# assertion 1's default render -- the arguments are identical, so a second render
# would only pay another full chart template for the same bytes.
env_has "$default_manifest" CURIE_API_KEY name \
  || fail "default install: dispatcher has no CURIE_API_KEY env; approval resolve calls will be rejected by the API"
env_has "$default_manifest" CURIE_API_KEY valueFrom.secretKeyRef \
  || fail "CURIE_API_KEY is not a secretKeyRef; an inline value would put the shared API key into 'helm get manifest' output"
actual="$(env_field "$default_manifest" CURIE_API_KEY valueFrom.secretKeyRef.name)"
[ "$actual" = "curie-secrets" ] \
  || fail "CURIE_API_KEY secretKeyRef names Secret '$actual', expected the chart Secret 'curie-secrets'"
actual="$(env_field "$default_manifest" CURIE_API_KEY valueFrom.secretKeyRef.key)"
[ "$actual" = "apiKey" ] \
  || fail "CURIE_API_KEY secretKeyRef uses key '$actual', expected the chart Secret's existing 'apiKey' key (the same key api.yaml consumes as API_KEY; a new key would let the two sides drift)"
if env_has "$default_manifest" CURIE_API_KEY value; then
  fail "CURIE_API_KEY renders an inline literal value; the credential must come from the Secret by reference only"
fi

# 5: unchanged gate -- no Slack tokens, no dispatcher.
out="$TMP/tokenless"
mkdir -p "$out"
helm template curie "$CHART" --output-dir "$out" >/dev/null
if [ -f "$out/curie/templates/dispatcher.yaml" ]; then
  fail "a token-less default install rendered a dispatcher Deployment; the curie.dispatcher.enabled gate regressed"
fi

echo "OK: dispatcher platform-API wiring render assertions passed"
