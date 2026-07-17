#!/usr/bin/env bash
#
# Render-assertion tests for the chart's rendered output.
#
# Issue #195 (auto-generate strong per-release chart credentials), Assertions
# 1-5. Proves three things about the chart's credential Secret:
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
# Issue #488 (freeze the boot env in the contract), Assertions 6-7. The Helm
# template cannot import the frozen contract crate, so its hand-typed boot-env
# names have no compiler holding them to it. Assertion 6 renders the runner
# container and holds its env names to the generated key export; Assertion 7 is
# the negative control proving Assertion 6 can fail.
#
# Runnable locally (from anywhere) and from CI. Fails loudly, naming the key.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$CHART/../.." && pwd)"

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

echo "=== Assertion 6: runner env names are declared boot-env keys (#488) ==="
# The chart is the one boot-env producer that cannot import the frozen contract:
# a Helm template has no way to reference agentos_aci_protocol, so its env names
# are hand-typed YAML. This render-assert is the only thing holding them to the
# contract, which is why it exists and why Assertion 7 proves it can fail.
#
# The expected key list is READ FROM the generated crate, never hand-copied here:
# a copy would be a third drift site and would defeat the point of the pin.
#
# Subset, never equality (issue #488, edge case 4): a warm/unbound pod
# legitimately carries only a few boot vars (AGENTOS_SESSION_ID is baked as
# `warm-unbound`, budget/tokens arrive per-claim from the worker), so requiring
# every exported key to be present would fail every render. What we can hold is
# the inverse: no runner env name that LOOKS like a boot key may be absent from
# the contract. That catches the real regression, a typo or a rename that the
# runner then silently never reads.
KEY_SRC="$REPO_ROOT/packages/aci-protocol/generated/rust/src/lib.rs"
[[ -f "$KEY_SRC" ]] || fail "generated key source not found at $KEY_SRC; run the contract codegen first."

ENV_CHECK="$TMP/check_runner_env.py"
cat > "$ENV_CHECK" <<'PYEOF'
"""Assert a rendered SandboxTemplate's runner env names are declared boot keys.

argv: <rendered-dir> <generated-rust-lib.rs>
Exits 0 on pass, 1 naming the offending key(s) on failure.
"""
import pathlib
import re
import sys

import yaml

# Env names in these namespaces are boot-env contract keys and must be declared.
# Anything else the runner container carries (HOME, and operator free-form
# extraEnv on a non-default render) is out of scope by design: extraEnv is
# operator-supplied and the contract does not govern it (issue #488, edge case 6).
CONTRACT_PREFIXES = ("AGENTOS_", "OTEL_EXPORTER_OTLP_", "ANTHROPIC_")

rendered, key_src = sys.argv[1], sys.argv[2]

# The exported list, parsed out of the generated `env_keys` module. Scoped to
# that module so an unrelated string constant elsewhere in the crate cannot
# widen the allowed set.
text = pathlib.Path(key_src).read_text()
module = re.search(r"pub mod env_keys \{(.*?)\n\}", text, re.S)
if not module:
    sys.stderr.write(f"no `pub mod env_keys` block found in {key_src}\n")
    sys.exit(1)
declared = set(re.findall(r'pub const [A-Z0-9_]+: &str = "([A-Z0-9_]+)"', module.group(1)))
if not declared:
    sys.stderr.write(f"`env_keys` in {key_src} exported no keys; the pin would be vacuous\n")
    sys.exit(1)

# Scope to the RUNNER container only (issue #488, edge case 5): the bundle-fetch
# and bundle-extract init containers declare their own AGENTOS_BUNDLE_REF, which
# is init-container env, not the runner's boot env.
found = {}
def walk(node, path):
    if isinstance(node, dict):
        if node.get("name") == "runner" and "image" in node:
            for entry in node.get("env", []) or []:
                found.setdefault(entry["name"], path)
        for key, value in node.items():
            walk(value, f"{path}/{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            walk(value, f"{path}[{i}]")

for path in sorted(pathlib.Path(rendered).rglob("*.yaml")):
    for doc in yaml.safe_load_all(path.read_text()):
        if isinstance(doc, dict) and doc.get("kind") == "SandboxTemplate":
            walk(doc, str(path))

if not found:
    sys.stderr.write("found no runner container env in the rendered SandboxTemplate; "
                     "the subset assert would pass vacuously\n")
    sys.exit(1)

# Non-vacuity floor: the render must actually carry boot env. Without this a
# template that dropped its whole env block would sail through the subset check.
contract_names = {n for n in found if n.startswith(CONTRACT_PREFIXES)}
if "AGENTOS_SESSION_ID" not in contract_names or len(contract_names) < 4:
    sys.stderr.write(
        "runner env does not carry a plausible boot env (expected AGENTOS_SESSION_ID "
        f"and 4+ contract-namespaced names); got {sorted(contract_names)}\n")
    sys.exit(1)

undeclared = sorted(n for n in contract_names if n not in declared)
if undeclared:
    for name in undeclared:
        sys.stderr.write(
            f"runner container env '{name}' (at {found[name]}) is NOT a declared "
            "boot-env key in aci_protocol.session.BootEnv. Either it is a typo of a "
            "real key, or the chart is inventing an env var the runner never reads.\n")
    sys.exit(1)

print(f"  ok: {len(contract_names)} runner boot-env names all declared: "
      f"{', '.join(sorted(contract_names))}")
PYEOF

# Render one chart dir's SandboxTemplate and check it. Returns nonzero (rather
# than exiting) so the negative control can assert the failure.
check_runner_env() {
  # $1 = chart dir, $2 = label, rest = extra helm args
  local chart="$1" label="$2"
  shift 2
  local out
  out="$(mktemp -d -p "$TMP")"
  # --output-dir, not a stdout pipe: a piped `helm template` can truncate at
  # exit 0, which would silently turn this assert into a false negative. No
  # --show-only either (it does not compose with --output-dir); the extractor
  # selects the SandboxTemplate by kind.
  helm template "$chart" --output-dir "$out" "$@" > /dev/null
  echo "  render: $label"
  python3 "$ENV_CHECK" "$out" "$KEY_SRC"
}

# Default render: fakeModel + a baked model + OTel, which is every literal on
# the default path.
check_runner_env "$CHART" "default values" \
  || fail "default render carries a runner env name that is not a declared boot-env key."
# Widened render: reaches the conditional literals the default branches past
# (AGENTOS_CREDENTIALS, and the inference-branch ANTHROPIC_BASE_URL/AGENTOS_MODEL).
check_runner_env "$CHART" "credentials + in-cluster inference" \
  --set agentSandbox.runner.credentials=dummy \
  --set inference.deploy=true \
  || fail "widened render carries a runner env name that is not a declared boot-env key."

echo "=== Assertion 7: negative control -- a misspelled runner env name FAILS ==="
# Mandatory: an assert that has never been shown failing is not a pin. Mutate a
# TEMP COPY of the chart (never the real template) and require the check to
# reject it, naming the bad key.
MUTANT="$TMP/mutant"
cp -a "$CHART" "$MUTANT"
python3 - "$MUTANT/templates/agent-sandbox.yaml" <<'PYEOF'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
text = p.read_text()
old, new = "- name: AGENTOS_SANDBOX_ID", "- name: AGENTOS_SANBOX_ID"
if old not in text:
    sys.stderr.write(f"negative control could not find {old!r} to mutate\n")
    sys.exit(1)
p.write_text(text.replace(old, new, 1))
PYEOF
if check_runner_env "$MUTANT" "mutant (AGENTOS_SANDBOX_ID -> AGENTOS_SANBOX_ID)" 2>&1; then
  fail "negative control did not fire: a misspelled 'AGENTOS_SANBOX_ID' passed the boot-env assert, so Assertion 6 is not actually pinning anything."
fi
echo "  ok: misspelled runner env name is rejected (the assert can fail)"

echo
echo "PASS: sealed render generates strong values for all 9 keys (encryptionKey 64-hex); dev overlay keeps published defaults; explicit override wins on the sealed path; every runner boot-env name is a declared contract key (proven by a failing negative control)."
