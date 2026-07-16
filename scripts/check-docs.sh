#!/usr/bin/env bash
# Regenerate the generated regions of the interface catalog from each seam's
# front-matter, fail if anything drifted, then lint every citation under the
# linted root. This is the local mirror of the CI docs gate and the exact shape
# of scripts/check-contracts.sh: regenerate, diff, then check. Run it after any
# intended catalog change, then commit the regenerated docs.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

generated_docs=(docs/interfaces.md docs/interfaces docs/adr/README.md)

# ADR numbers must be unique (#521). Two branches each adding "the next ADR
# number" merge clean in git -- git sees two unrelated new files, not a conflict
# -- yet collide in the tree, making "ADR-00NN" ambiguous in every citation and
# breaking the supersession chain. Only a live check catches it, so gate it here.
echo "== checking ADR numbers are unique =="
dupes="$(
  for f in docs/adr/[0-9][0-9][0-9][0-9]-*.md; do
    basename "$f" | grep -oE '^[0-9]{4}'
  done | sort | uniq -d
)"
if [ -n "$dupes" ]; then
  echo "ERROR: duplicate ADR number(s) in docs/adr/:" >&2
  echo "$dupes" | sed 's/^/  /' >&2
  echo "Renumber the newer colliding ADR and fix inbound citations (#521)." >&2
  exit 1
fi

echo "== regenerating the seam table, the ADR index, and per-doc headers =="
uv run python -m agentos_doclint --repo-root "$repo_root" --write

echo "== checking for drift =="
if ! git diff --exit-code -- "${generated_docs[@]}"; then
  echo "ERROR: generated catalog regions drifted from the seam front-matter." >&2
  echo "The files above were regenerated and differ. Review, then commit them." >&2
  exit 1
fi

echo "== linting citations under the linted root =="
uv run python -m agentos_doclint --repo-root "$repo_root"

echo "OK: the interface catalog is generated, drift-free, and every citation resolves."
