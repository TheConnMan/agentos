#!/usr/bin/env bash
# One-command bootstrap/update for an agentos source checkout.
#
# Solves the bootstrap chicken-and-egg: the `agentos` CLI cannot install itself
# because it does not exist until it is built. On a fresh checkout this script
# runs the single pre-binary step -- `cargo install`, which builds AND drops
# `agentos` on PATH (~/.cargo/bin) -- and then hands off to `agentos install`
# for the rest (uv sync, pnpm install, runner image). On an existing install it
# reuses already-present heavyweight artifacts (via `agentos install --update`)
# but always rebuilds and reinstalls the CLI from this checkout.
#
#   git clone <repo> && cd agentos && ./install.sh
#
set -euo pipefail

cd "$(dirname "$0")"

# Invoke the freshly installed binary by its absolute path so this works even
# when ~/.cargo/bin is not yet on PATH in the current shell (a new clone on a
# machine whose shell rc has not been re-sourced).
CARGO_BIN="${CARGO_HOME:-$HOME/.cargo}/bin"
AGENTOS_BIN="$CARGO_BIN/agentos"

# Always (re)build and install the CLI from this checkout. A file-mtime heuristic
# cannot tell whether the checked-out source matches the installed binary -- a
# `git checkout`/branch switch changes content without reliably bumping mtimes,
# so a stale binary (missing commands that exist in the source) would silently
# survive. cargo's own caching keeps this to a few seconds when nothing changed;
# --force just re-links and copies the binary into place.
UPDATE_ARGS=()
[[ -x "$AGENTOS_BIN" ]] && UPDATE_ARGS=(--update)
echo "==> cargo install --path cli --force (build agentos and install it to ~/.cargo/bin)"
cargo install --path cli --force

echo "==> agentos install ${UPDATE_ARGS[*]} (deps + runner image as needed)"
"$AGENTOS_BIN" install "${UPDATE_ARGS[@]}"

echo
echo "Done. If 'agentos' is not found in this shell, add ~/.cargo/bin to PATH"
echo "(rustup writes ~/.cargo/env for this) and open a new shell."
echo
echo "For future changes you don't need this script -- run 'agentos update' to"
echo "rebuild and reinstall the CLI on PATH ('agentos update --image' also rebuilds"
echo "the runner image)."
