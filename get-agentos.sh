#!/usr/bin/env bash
# One-command verified installer for the released agentos CLI.
#
#   curl -fsSL https://raw.githubusercontent.com/curie-eng/agentos/main/get-agentos.sh | bash
#
# This automates the flow in docs/release-verification.md without weakening it:
# it resolves the latest release (or AGENTOS_VERSION=vX.Y.Z), downloads the
# right asset for this platform plus checksums.txt and its sigstore bundle,
# ALWAYS verifies the sha256, runs `cosign verify-blob` with the pinned
# certificate identity when cosign is on PATH (set AGENTOS_REQUIRE_COSIGN=1 to
# make its absence a hard failure), and installs the binary to a PATH location.
# It never calls sudo; run it under sudo yourself if you want a root-owned dir.
set -euo pipefail

REPO=curie-eng/agentos

# Resolve the asset for this machine. The release ships exactly two binaries --
# Linux x86_64 and macOS Apple silicon -- so this case statement is the whole
# platform contract. Reset ASSET first so an unmatched platform is empty, not
# stale, and name neither asset literally anywhere else (issues #746, #752).
ASSET=
case "$(uname -s)/$(uname -m)" in
  Linux/x86_64)                ASSET=agentos-x86_64-unknown-linux-gnu ;;
  Darwin/arm64|Darwin/aarch64) ASSET=agentos-aarch64-apple-darwin ;;
esac
if [ -z "$ASSET" ]; then
  echo "error: no prebuilt agentos binary for $(uname -s)/$(uname -m)." >&2
  echo "Supported: Linux x86_64 and macOS Apple silicon (Darwin arm64)." >&2
  echo "On anything else, build the CLI from source (see cli/ and docs/release-verification.md)." >&2
  exit 1
fi

# Pick the sha256 tool up front: sha256sum on Linux, shasum -a 256 on macOS.
sha256_check() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum --check --ignore-missing "$@"
  else
    shasum -a 256 --check --ignore-missing "$@"
  fi
}
if ! command -v sha256sum >/dev/null 2>&1 && ! command -v shasum >/dev/null 2>&1; then
  echo "error: need sha256sum or shasum on PATH to verify the download." >&2
  exit 1
fi

# Resolve the release tag: AGENTOS_VERSION wins, else the GitHub API's latest.
VERSION="${AGENTOS_VERSION:-}"
if [ -z "$VERSION" ]; then
  VERSION="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
    | grep '"tag_name"' | head -1 | cut -d'"' -f4)"
fi
if [ -z "$VERSION" ]; then
  echo "error: could not resolve the latest release tag. Set AGENTOS_VERSION=vX.Y.Z." >&2
  exit 1
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
cd "$WORKDIR"

BASE="https://github.com/$REPO/releases/download/$VERSION"
echo "==> downloading $ASSET ($VERSION)"
curl -fsSLO "$BASE/$ASSET"
curl -fsSLO "$BASE/checksums.txt"
curl -fsSLO "$BASE/checksums.txt.sigstore.json"

# Step 1: prove checksums.txt is genuinely this repo's release output. Keep this
# and the sha256 step as separate statements so `set -e` still trips on either;
# do not chain them with && (that would swallow the second under set -e).
if command -v cosign >/dev/null 2>&1; then
  echo "==> cosign verify-blob (signed checksums manifest)"
  cosign verify-blob \
    --bundle checksums.txt.sigstore.json \
    --certificate-identity "https://github.com/$REPO/.github/workflows/release.yaml@refs/tags/$VERSION" \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com \
    checksums.txt
elif [ "${AGENTOS_REQUIRE_COSIGN:-0}" = "1" ]; then
  echo "error: AGENTOS_REQUIRE_COSIGN=1 but cosign is not on PATH." >&2
  echo "Install cosign: https://docs.sigstore.dev/system_config/installation/" >&2
  exit 1
else
  echo "WARNING: cosign not found -- skipping signature check of checksums.txt." >&2
  echo "  The sha256 check below still runs, but cannot prove the manifest is genuine." >&2
  echo "  Verify the signature by hand once cosign is installed:" >&2
  echo "    cosign verify-blob --bundle checksums.txt.sigstore.json \\" >&2
  echo "      --certificate-identity https://github.com/$REPO/.github/workflows/release.yaml@refs/tags/$VERSION \\" >&2
  echo "      --certificate-oidc-issuer https://token.actions.githubusercontent.com checksums.txt" >&2
  echo "  Or re-run with AGENTOS_REQUIRE_COSIGN=1 to make this a hard failure." >&2
fi

# Step 2: prove the binary matches the (now verified) manifest.
echo "==> verifying sha256"
sha256_check checksums.txt

# Step 3: install. /usr/local/bin when writable without sudo, else ~/.local/bin.
if [ -w /usr/local/bin ]; then
  INSTALL_DIR=/usr/local/bin
else
  INSTALL_DIR="$HOME/.local/bin"
  mkdir -p "$INSTALL_DIR"
fi
# Set an explicit 0755 rather than chmod +x: under a permissive umask the
# downloaded file can start group/world-writable, and +x would preserve those
# write bits into the installed binary (mv keeps the mode), letting another
# local user replace the CLI later run as you.
chmod 755 "$ASSET"
mv -f "$ASSET" "$INSTALL_DIR/agentos"
echo "==> installed agentos to $INSTALL_DIR/agentos"

case ":$PATH:" in
  *":$INSTALL_DIR:"*) ;;
  *)
    echo "WARNING: $INSTALL_DIR is not on your PATH. Add it, e.g.:" >&2
    echo "    export PATH=\"$INSTALL_DIR:\$PATH\"" >&2
    ;;
esac

"$INSTALL_DIR/agentos" --version
