#!/usr/bin/env bash
# One-command bootstrap for an agentos source checkout.
#
# Solves the bootstrap chicken-and-egg: the `agentos` CLI cannot install itself
# because it does not exist until it is built. This script runs the single
# pre-binary step -- `cargo install`, which builds AND drops `agentos` on PATH
# (~/.cargo/bin) -- and then hands off to `agentos install` for the rest (uv
# sync, pnpm install, runner image). Run it once, right after cloning:
#
#   git clone <repo> && cd agentos && ./install.sh
#
set -euo pipefail

cd "$(dirname "$0")"

echo "==> cargo install --path cli (builds agentos and installs it to ~/.cargo/bin)"
cargo install --path cli --force

# Invoke the freshly installed binary by its absolute path so this works even
# when ~/.cargo/bin is not yet on PATH in the current shell (a new clone on a
# machine whose shell rc has not been re-sourced).
CARGO_BIN="${CARGO_HOME:-$HOME/.cargo}/bin"
echo "==> agentos install (deps + runner image)"
"$CARGO_BIN/agentos" install

echo
echo "Done. If 'agentos' is not found in this shell, add ~/.cargo/bin to PATH"
echo "(rustup writes ~/.cargo/env for this) and open a new shell."
