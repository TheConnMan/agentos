# 52. The release trust model: one signed manifest, closed-world coverage

Date: 2026-07-17

Status: Accepted

Implements [#629](https://github.com/curie-eng/agentos/issues/629).
Scoped to downloadable GitHub release assets. Signing, provenance, and SBOMs for
the GHCR container images are [#62](https://github.com/curie-eng/agentos/issues/62)
and are deliberately not decided here.

## Context

A release publishes the `agentos` CLI binaries, the packaged Helm chart, and
`compose.release.yaml`. None of them carried a checksum, a signature, provenance,
or an SBOM. The documented install path was `curl` the binary, `chmod +x`, `sudo
mv` it onto `PATH`, and immediately point it at a cluster -- so the first thing a
new user did with AgentOS was run an unverifiable binary as root. Nothing let
them answer "is this the artifact that repo's workflow built?", and nothing let
us answer it either after the fact.

The obvious shape -- sign each asset, publish a `.sig` beside it -- is not the
only one, and the choices interact. Three questions had to be answered together:
what gets signed, what counts as covered, and when the check runs.

## Decision

**One signature, over the checksum manifest.** The release publishes
`checksums.txt` covering every file, signs *that* with keyless cosign, and
attests SLSA build provenance over every file in `dist` (the manifest included).
Per-asset signatures were the alternative; they were rejected as redundant. The
chain already closes without them: provenance and the cosign certificate bind
`checksums.txt` to this repo, this workflow, and the release commit, and the
manifest binds each file to itself. A user who verifies the manifest and then
checks one file against it has verified that file, and the docs are one procedure
rather than one per asset type.

**Keyless, not a signing key.** Both cosign and the attestation mint a
short-lived OIDC identity for the workflow run. There is no long-lived private
key to store, rotate, or leak, and the identity a verifier pins is the workflow
path plus the tag -- a fact about *who built it*, which is the actual question,
rather than *who holds the key*.

**Coverage is closed-world.** The gate does not check a list of assets we know
about today. Every file in the release must be a known asset, an SBOM for one, or
the manifest and its signature; anything else fails. An allowlist would go
quietly stale the first time someone adds an asset -- the gate would still report
green while shipping the new artifact unattested, which is the exact failure this
ADR exists to prevent. The cost is that adding an asset means teaching the gate
about it. That cost is the point.

**The gate runs before signing, and again after publishing.** Before, because
cosign will sign an incomplete manifest without complaint, laundering a coverage
gap into a valid signature: `release/integrity.py` refuses to *build* a manifest
over an incomplete dist, so the release fails closed. After, because what we
uploaded is not evidence about what users download -- the `verify-release` job
re-downloads the published release and re-runs the whole documented path
(checksums, `cosign verify-blob`, `gh attestation verify`) against the real
bytes. That second run is also what keeps
[`docs/release-verification.md`](../release-verification.md) honest: if the
documented commands stop working, the release goes red.

**SBOM scope follows the artifact.** The CLI binaries are cataloged from the
`cli` source tree, because `Cargo.lock` is what actually names every crate that
went in and stripping removes what a binary scan would need. The chart and the
compose file are deployment manifests with no dependencies of their own, so their
SBOMs inventory the packaged artifact itself; the dependency graph of what they
deploy belongs to the images they pin, and ships with those images (#62). This is
the weakest part of the decision and is recorded as such: a chart SBOM that names
only the chart is close to a restatement of its checksum.

## Consequences

- Verifying a release needs `cosign`, or `gh` for the one-command
  `gh attestation verify` path. Both are documented; neither is bundled.
- The README pins an explicit version in its verification snippet, because the
  cosign certificate identity names the exact tag. A wildcard identity would
  accept any tag's manifest and defeat the pin, so the snippet goes stale by
  design at each release rather than being loosened.
- A release binary still fetches the chart and compose assets itself at `cluster
  up` / `local up` and does **not** verify them; that fetch is protected by HTTPS
  to GitHub, not by this signature. Closing that is follow-on work -- the trust
  material now exists for the CLI to check, which it did not before.
- Apple signing and notarization remain deferred pending an Apple account
  decision. The macOS binary is covered by everything above; it is Gatekeeper,
  not verifiability, that is missing.
