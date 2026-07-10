#!/usr/bin/env bash
#
# Render-assertion test for issue #253 (BYO model: values-gated LiteLLM
# Anthropic-format sidecar in the chart). Proves:
#
#   1. DEFAULT render (agentSandbox.runner.liteLLM.enabled defaults false): the
#      sidecar container, its config volume, and the localhost ANTHROPIC_BASE_URL
#      override are ALL absent from the rendered SandboxTemplate.
#   2. ENABLED render (liteLLM.enabled=true + a configExistingSecret): the
#      `litellm` sidecar container renders, mounts the operator Secret at
#      /etc/litellm/config.yaml, and the runner's ANTHROPIC_BASE_URL is repointed
#      at http://localhost:<port>.
#   3. PRECEDENCE: with local inference.deploy=true the sidecar's localhost
#      ANTHROPIC_BASE_URL override does NOT render (the in-cluster inference
#      endpoint path wins), matching the values-doc contract.
#
# Runnable locally (from anywhere) and from CI. Fails loudly.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART="$(cd "$SCRIPT_DIR/.." && pwd)"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

TPL=templates/agent-sandbox.yaml

fail() { echo "FAIL: $*" >&2; exit 1; }

render() {
  # $@ = extra --set flags. agentSandbox.deploy is on by default in values.yaml.
  helm template rel "$CHART" --show-only "$TPL" "$@"
}

echo "=== Assertion 1: DEFAULT render omits the sidecar ==="
DEFAULT="$TMP/default.yaml"
render > "$DEFAULT"
if grep -qE '^\s*-?\s*name: litellm' "$DEFAULT"; then
  fail "default render must NOT contain a litellm sidecar/volume; found one (sidecar rendered while disabled)."
fi
if grep -q "localhost:" "$DEFAULT"; then
  fail "default render must NOT repoint ANTHROPIC_BASE_URL at localhost."
fi
echo "  ok: no litellm sidecar, no localhost base-url override by default"

echo "=== Assertion 2: ENABLED render adds the sidecar + repoints the runner ==="
ENABLED="$TMP/enabled.yaml"
render \
  --set agentSandbox.runner.liteLLM.enabled=true \
  --set agentSandbox.runner.liteLLM.configExistingSecret=my-litellm-cfg \
  --set agentSandbox.runner.liteLLM.port=4000 > "$ENABLED"

grep -qE '^\s*- name: litellm$' "$ENABLED" \
  || fail "enabled render must contain a container named 'litellm'."
grep -q 'secretName: "my-litellm-cfg"' "$ENABLED" \
  || fail "enabled render must mount the operator config Secret (my-litellm-cfg)."
grep -q 'mountPath: /etc/litellm' "$ENABLED" \
  || fail "enabled render must mount the config at /etc/litellm."
grep -q 'value: http://localhost:4000' "$ENABLED" \
  || fail "enabled render must repoint ANTHROPIC_BASE_URL at http://localhost:4000."
echo "  ok: sidecar container, config Secret mount, and localhost base-url all present"

echo "=== Assertion 3: local inference wins over the sidecar base-url override ==="
INFER="$TMP/infer.yaml"
render \
  --set agentSandbox.runner.liteLLM.enabled=true \
  --set agentSandbox.runner.liteLLM.configExistingSecret=my-litellm-cfg \
  --set inference.deploy=true > "$INFER"
if grep -q 'value: http://localhost:' "$INFER"; then
  fail "with inference.deploy=true the sidecar localhost ANTHROPIC_BASE_URL must NOT render (inference path wins)."
fi
echo "  ok: inference.deploy=true suppresses the localhost base-url override"

echo
echo "PASS: LiteLLM sidecar is off by default, renders fully when enabled, and yields to local inference."
