#!/usr/bin/env bash
#
# Render-assertion test for issue #195 (auto-generate strong per-release chart
# credentials). Proves three things about the chart's credential Secret:
#
#   1. A SEALED render (security.allowDevDefaults defaults false, `lookup` empty
#      offline under `helm template`) GENERATES a strong random value for each of
#      the nine chart-owned secret keys instead of shipping the published dev
#      default. The generated langfuseEncryptionKey is 64 lowercase-hex chars.
#   2. The DEV overlay (values-dev.yaml sets allowDevDefaults=true) keeps the
#      deterministic published defaults, so the dev/e2e path renders unchanged.
#   3. An explicit `--set` override that differs from the published default is
#      honored on the sealed path (override wins over generation).
#
# Runnable locally (from anywhere) and from CI. Fails loudly, naming the key.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART="$(cd "$SCRIPT_DIR/.." && pwd)"

# The nine chart-owned secret keys, each with its published dev default.
KEYS=(
  postgresPassword
  valkeyPassword
  clickhousePassword
  minioRootPassword
  langfuseSalt
  langfuseEncryptionKey
  langfuseNextauthSecret
  apiKey
  githubWebhookSecret
)
declare -A DEFAULTS=(
  [postgresPassword]="postgres"
  [valkeyPassword]="valkeypass"
  [clickhousePassword]="clickhouse"
  [minioRootPassword]="miniosecret"
  [langfuseSalt]="dev-salt-change-me"
  [langfuseEncryptionKey]="0000000000000000000000000000000000000000000000000000000000000000"
  [langfuseNextauthSecret]="dev-nextauth-secret-change-me"
  [apiKey]="agentos-dev-key"
  [githubWebhookSecret]="dev-webhook-secret"
)

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

SEALED="$TMP/sealed.yaml"
DEV="$TMP/dev.yaml"

echo "=== Rendering sealed chart (allowDevDefaults default false) ==="
helm template "$CHART" --show-only templates/secrets.yaml > "$SEALED"

echo "=== Rendering dev overlay (allowDevDefaults=true) ==="
helm template "$CHART" -f "$CHART/values-dev.yaml" --show-only templates/secrets.yaml > "$DEV"

# Read one stringData key from a rendered Secret via PyYAML (robust vs grep/awk).
read_key() {
  # $1 = rendered secret YAML file, $2 = key
  python3 -c '
import sys, yaml
path, key = sys.argv[1], sys.argv[2]
doc = yaml.safe_load(open(path))
sd = (doc or {}).get("stringData", {})
if key not in sd:
    sys.stderr.write("stringData is missing key %r\n" % key)
    sys.exit(3)
sys.stdout.write(str(sd[key]))
' "$1" "$2"
}

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

echo "=== Assertion 1: sealed render GENERATES (no published default) ==="
for key in "${KEYS[@]}"; do
  val="$(read_key "$SEALED" "$key")"
  def="${DEFAULTS[$key]}"
  if [[ "$val" == "$def" ]]; then
    fail "sealed render still emits the published dev default for '$key' (value == '$def'); expected a generated value."
  fi
  echo "  ok: $key was generated (not the published default)"
done

echo "=== Assertion 2: sealed langfuseEncryptionKey is 64 lowercase-hex chars ==="
enc="$(read_key "$SEALED" langfuseEncryptionKey)"
if [[ ! "$enc" =~ ^[0-9a-f]{64}$ ]]; then
  fail "sealed langfuseEncryptionKey must match ^[0-9a-f]{64}$; got '${enc}' (length ${#enc})."
fi
echo "  ok: langfuseEncryptionKey is 64 lowercase-hex chars"

echo "=== Assertion 3: dev overlay keeps the deterministic published defaults ==="
for key in "${KEYS[@]}"; do
  val="$(read_key "$DEV" "$key")"
  def="${DEFAULTS[$key]}"
  if [[ "$val" != "$def" ]]; then
    fail "dev overlay must keep the published default for '$key'; expected '$def', got '$val'."
  fi
  echo "  ok: $key == published default (deterministic dev path)"
done

echo "=== Assertion 4: explicit override wins on the sealed path ==="
# On the sealed path (no allowDevDefaults, empty offline `lookup`), an operator
# `--set` that differs from the published default must be honored verbatim rather
# than generated -- this proves the override branch sits ahead of generation.
OVERRIDE="$TMP/override.yaml"
helm template "$CHART" --set api.apiKey=override-sentinel-xyz \
  --show-only templates/secrets.yaml > "$OVERRIDE"
got="$(read_key "$OVERRIDE" apiKey)"
if [[ "$got" != "override-sentinel-xyz" ]]; then
  fail "explicit --set api.apiKey override must be honored on the sealed path; expected 'override-sentinel-xyz', got '$got'."
fi
echo "  ok: explicit apiKey override honored (override wins over generation)"

echo "=== Assertion 5: quoted \"false\" does NOT disable generation (fail closed) ==="
# Go templates treat any non-empty string as truthy, so a quoted
# `security.allowDevDefaults="false"` (easily produced by --set or values-file
# quoting) must NOT read as truthy and ship the published dev default. Only the
# literal `true` opts into defaults; every other value falls through to
# generation. Assert both the bareword and the quoted-string spellings still
# generate apiKey (not the published `agentos-dev-key`).
for spelling in "security.allowDevDefaults=false" 'security.allowDevDefaults="false"'; do
  FALSY="$TMP/falsy.yaml"
  helm template "$CHART" --set "$spelling" \
    --show-only templates/secrets.yaml > "$FALSY"
  got="$(read_key "$FALSY" apiKey)"
  if [[ "$got" == "agentos-dev-key" ]]; then
    fail "allowDevDefaults '$spelling' must NOT ship the published default; apiKey generated expected, got 'agentos-dev-key' (fail-OPEN regression)."
  fi
  echo "  ok: --set $spelling still generates apiKey (fail closed)"
done

echo
echo "PASS: sealed render generates strong values for all 9 keys (encryptionKey 64-hex); dev overlay keeps published defaults; explicit override wins on the sealed path."
