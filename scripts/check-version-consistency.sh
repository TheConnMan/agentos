#!/usr/bin/env bash
#
# Assert the release-coupled version fields agree (#489).
#
# Cutting a release hand-syncs three fields plus a git tag:
#   - cli/Cargo.toml            version
#   - charts/agentos/Chart.yaml version
#   - charts/agentos/Chart.yaml appVersion
# The release workflow re-derives the chart version from the tag, so a stale
# committed appVersion still ships a "correct" release artifact while every
# consumer of the COMMITTED chart (helm template, `agentos cluster up`) pulls the
# wrong runner image tag (appVersion is the image-tag fallback). This gate catches
# that drift on every PR and, reused by the release workflow, at tag time.
#
# Exits 0 when all three agree, 1 (naming the mismatch) otherwise. Prints the
# agreed version on success so callers can capture it.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cargo_version="$(grep -m1 '^version = ' "$repo_root/cli/Cargo.toml" | sed -E 's/^version = "(.*)"/\1/')"
chart_version="$(grep -m1 '^version:' "$repo_root/charts/agentos/Chart.yaml" | sed -E 's/^version:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/')"
chart_app_version="$(grep -m1 '^appVersion:' "$repo_root/charts/agentos/Chart.yaml" | sed -E 's/^appVersion:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/')"

fail=0
if [ "$cargo_version" != "$chart_version" ]; then
  echo "MISMATCH: cli/Cargo.toml version ($cargo_version) != Chart.yaml version ($chart_version)" >&2
  fail=1
fi
if [ "$cargo_version" != "$chart_app_version" ]; then
  echo "MISMATCH: cli/Cargo.toml version ($cargo_version) != Chart.yaml appVersion ($chart_app_version)" >&2
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  echo "The release-coupled versions must agree. Run 'agentos dev bump-version <X.Y.Z>' to set all three." >&2
  exit 1
fi

echo "version-consistency OK: cli/Cargo.toml, Chart.yaml version, and appVersion all = $cargo_version"
