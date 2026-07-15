#!/usr/bin/env bash
#
# Render-assertion test for per-agent connector secrets (ADR-0009, #429). Proves,
# with `helm template` alone (no cluster), that:
#
#   1. Each `agentSandbox.connectorSecrets.<agent>` entry renders its OWN Opaque
#      Secret named <fullname>-agent-<agent>-connector-secrets, labelled
#      agentos.dev/agent=<agent>, carrying that agent's named values.
#   2. Those values are NOT merged into the shared chart Secret
#      (<fullname>) -- one agent's connector token must not be readable by every
#      component.
#   3. With no connectorSecrets (the default) NO connector Secret renders, so a
#      stock install is unaffected (fail-closed / no surprise objects).
#
# Fails loudly, naming the assertion. Runnable locally and from CI.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART="$(cd "$SCRIPT_DIR/.." && pwd)"

fail() {
  echo "ASSERTION FAILED: $1" >&2
  exit 1
}

# 1 + 2: a configured agent renders its own Secret with the value, and the shared
# Secret does not carry it.
rendered="$(helm template agentos "$CHART" \
  --set 'agentSandbox.connectorSecrets.github-issues.GITHUB_PERSONAL_ACCESS_TOKEN=ghp_assert' 2>/dev/null)"

per_agent="$(printf '%s' "$rendered" | awk '
  /^# Source: agentos\/templates\/agent-connector-secrets.yaml/ {inblock=1}
  /^# Source:/ && !/agent-connector-secrets/ {inblock=0}
  inblock {print}
')"

printf '%s' "$per_agent" | grep -q "name: agentos-agent-github-issues-connector-secrets" \
  || fail "per-agent Secret 'agentos-agent-github-issues-connector-secrets' did not render"
printf '%s' "$per_agent" | grep -q 'agentos.dev/agent: "github-issues"' \
  || fail "per-agent Secret missing the agentos.dev/agent label"
printf '%s' "$per_agent" | grep -q 'GITHUB_PERSONAL_ACCESS_TOKEN: "ghp_assert"' \
  || fail "per-agent Secret missing the connector value"

# The shared chart Secret (Source secrets.yaml) must NOT carry the connector key.
shared="$(printf '%s' "$rendered" | awk '
  /^# Source: agentos\/templates\/secrets.yaml/ {inblock=1}
  /^# Source:/ && !/templates\/secrets.yaml/ {inblock=0}
  inblock {print}
')"
if printf '%s' "$shared" | grep -q "GITHUB_PERSONAL_ACCESS_TOKEN"; then
  fail "connector secret leaked into the shared chart Secret"
fi

# 3: no connectorSecrets -> no connector Secret object at all.
default_render="$(helm template agentos "$CHART" 2>/dev/null)"
if printf '%s' "$default_render" | grep -q "connector-secrets"; then
  fail "a connector Secret rendered with no agentSandbox.connectorSecrets configured"
fi

echo "OK: per-agent connector-secret render assertions passed"
