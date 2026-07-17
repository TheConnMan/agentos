# Verifying a release

Every GitHub release publishes the `agentos` CLI binaries, the Helm chart, and
`compose.release.yaml`. Installing the CLI means running a downloaded binary as
root and pointing it at a cluster, so verify what you received before you run it.

This page is the reference. The [README quickstart](../README.md#quickstart) has
the short copy-paste version for the default install path.

## What each release publishes

| Asset | What it is |
|---|---|
| `agentos-x86_64-unknown-linux-gnu` | CLI binary, Linux x86_64 |
| `agentos-aarch64-apple-darwin` | CLI binary, macOS Apple silicon |
| `agentos-<version>.tgz` | packaged Helm chart |
| `compose.release.yaml` | the self-contained local stack |
| `<asset>.spdx.json` | SPDX SBOM, one per asset |
| `checksums.txt` | sha256 of every file above |
| `checksums.txt.sigstore.json` | cosign signature over `checksums.txt` |

Every asset also carries [SLSA build provenance](https://slsa.dev/), naming the
repository, the workflow, and the commit it was built from.

The trust chain is: provenance and the cosign signature establish that
`checksums.txt` came from this repo's release workflow, and `checksums.txt`
establishes that each file is the one that workflow produced. So verifying the
manifest's signature and then checking a file against the manifest is enough --
you do not need a separate signature per asset.

## Verify the CLI before installing it

Set the version you are installing and download the binary alongside the manifest
and its signature:

```bash
VERSION=v0.4.0
REPO=curie-eng/agentos
ASSET=agentos-x86_64-unknown-linux-gnu   # macOS: agentos-aarch64-apple-darwin
BASE="https://github.com/$REPO/releases/download/$VERSION"

curl -fsSLO "$BASE/$ASSET"
curl -fsSLO "$BASE/checksums.txt"
curl -fsSLO "$BASE/checksums.txt.sigstore.json"
```

**Step 1 -- the manifest is genuinely ours.** Requires
[cosign](https://docs.sigstore.dev/system_config/installation/). The identity is
pinned to the exact workflow and tag, so a manifest signed by any other workflow,
repo, or ref fails:

```bash
cosign verify-blob \
  --bundle checksums.txt.sigstore.json \
  --certificate-identity "https://github.com/$REPO/.github/workflows/release.yaml@refs/tags/$VERSION" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  checksums.txt
```

**Step 2 -- the binary matches the manifest.** `--ignore-missing` checks only the
files you actually downloaded:

```bash
sha256sum --check --ignore-missing checksums.txt   # macOS: shasum -a 256 --check --ignore-missing checksums.txt
```

**Step 3 -- only now, install it.** Both checks must print OK first:

```bash
chmod +x "$ASSET" && sudo mv "$ASSET" /usr/local/bin/agentos
agentos --version
```

## Verify provenance with the GitHub CLI

If you have `gh` and would rather not install cosign, `gh attestation verify`
checks any single asset in one command, no manifest needed. It fails unless the
asset was built by this repo's release workflow:

```bash
gh attestation verify agentos-x86_64-unknown-linux-gnu \
  --repo curie-eng/agentos \
  --signer-workflow curie-eng/agentos/.github/workflows/release.yaml
```

Add `--source-ref "refs/tags/$VERSION"` to also pin it to a specific release, or
`--format json` to read the full provenance statement, including the commit the
build ran from.

## Verify the chart and the compose file

Same two steps, different asset. The chart and compose file are data rather than
executables, but a tampered chart deploys tampered images:

```bash
curl -fsSLO "$BASE/compose.release.yaml"
curl -fsSLO "$BASE/agentos-${VERSION#v}.tgz"
sha256sum --check --ignore-missing checksums.txt
```

Run the cosign step above first if you have not already: checking a file against
an unverified manifest proves only that it downloaded intact.

A release binary fetches these two assets itself when you run `agentos cluster
up` or `agentos local up`, caching them under `~/.cache/agentos/`. That fetch
does not verify them today: it is protected by HTTPS to GitHub, not by the
signature. Verify them by hand as above if you need the stronger guarantee.

## SBOMs

Each asset ships an SPDX 2.3 SBOM at `<asset>.spdx.json`, covered by
`checksums.txt` like any other file, so verify it the same way before trusting
it:

- **CLI binaries** -- cataloged from the `cli` source tree, so the SBOM is the
  full crate dependency graph that `Cargo.lock` pins. This is the one to feed to
  a vulnerability scanner.
- **Chart and compose** -- these are deployment manifests with no dependencies of
  their own; their SBOMs inventory the packaged artifact itself. The dependency
  graph of what they deploy belongs to the `agentos-*` container images, which
  carry their own SBOMs (issue #62).

Scan one with any SPDX-aware tool, for example
[grype](https://github.com/anchore/grype):

```bash
grype sbom:./agentos-x86_64-unknown-linux-gnu.spdx.json
```

## What happens if an asset is not covered

The release fails, before and after publishing.

`release/integrity.py` is the gate and the single definition of what "covered"
means. `.github/workflows/release.yaml` calls it twice: once before signing, to
refuse to build a checksum manifest over an incomplete asset set (signing an
incomplete manifest would launder the gap into a valid signature), and once
after publishing, in the `verify-release` job, which re-downloads the release and
re-runs the whole documented path -- checksums, cosign, and `gh attestation
verify` -- against the bytes users will actually get.

The check is closed-world: every file in the release must be a known asset, an
SBOM for one, or the manifest and its signature. A new asset added to the release
without an SBOM fails the gate rather than shipping unattested. When it fires,
add SBOM generation to the job that builds the asset -- do not widen the gate.

## Not covered yet

- **Apple notarization.** The macOS binary is unsigned and un-notarized, pending
  an Apple developer account decision. Gatekeeper quarantines a browser-downloaded
  copy; see the [README note](../README.md#quickstart). Verify it with cosign or
  `gh attestation verify` as above -- that is the real check regardless.
- **Container images.** GHCR image signing, provenance, and SBOMs are issue #62.
