#!/usr/bin/env bash
# One-command bootstrap/update for an agentos source checkout.
#
# Solves the bootstrap chicken-and-egg: the `agentos` CLI cannot install itself
# because it does not exist until it is built. On a fresh checkout this script
# runs the single pre-binary step -- `cargo install`, which builds AND drops
# `agentos` on PATH (~/.cargo/bin) -- and then hands off to `agentos install`
# for the rest (uv sync, pnpm install, runner image). On an existing install it
# only refreshes the CLI when the source tree is newer than the installed binary,
# then runs `agentos install --update` so already-present artifacts are reused.
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

cli_sources_newer_than_binary() {
  [[ ! -x "$AGENTOS_BIN" ]] && return 0
  find cli -path cli/target -prune -o -type f -newer "$AGENTOS_BIN" -print -quit | grep -q .
}

UPDATE_ARGS=()
if [[ -x "$AGENTOS_BIN" ]]; then
  UPDATE_ARGS=(--update)
  if cli_sources_newer_than_binary; then
    echo "==> updating agentos CLI (source is newer than $AGENTOS_BIN)"
    cargo install --path cli --force
  else
    echo "==> agentos CLI is already current at $AGENTOS_BIN"
  fi
else
  echo "==> cargo install --path cli (builds agentos and installs it to ~/.cargo/bin)"
  cargo install --path cli --force
fi

echo "==> agentos install ${UPDATE_ARGS[*]} (deps + runner image as needed)"
"$AGENTOS_BIN" install "${UPDATE_ARGS[@]}"

echo
echo "Done. If 'agentos' is not found in this shell, add ~/.cargo/bin to PATH"
echo "(rustup writes ~/.cargo/env for this) and open a new shell."
