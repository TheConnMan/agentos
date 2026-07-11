#!/usr/bin/env bash
# Run the primer before-after harness smoke: the deterministic fake-driver
# run that proves the harness wiring works without spending model tokens.
# The real before-after benchmark is `uv run python -m harness_eval run
# --driver claude` (see the harness-eval package).
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"
uv run python -m harness_eval run --driver fake
