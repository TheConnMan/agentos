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

**Coverage is closed-world.** Every file in the release must be an asset
`release/integrity.py` declares, a usable SBOM for one of them, or the manifest
and its signature; anything else fails. Checking only that the assets we know
about are covered would let the next asset ship uncovered while the gate reported
green. Nor is "has an SBOM" sufficient on its own: `files: dist/*` publishes
whatever is staged, so a stray file -- a leftover build artifact, or anything a
compromised step drops into dist -- would otherwise be signed and attested purely
for bringing an SBOM along. The cost is that adding an asset means declaring it,
as a reviewed edit. That cost is the point.

**Publication is the consequence of verification, not a step before it.** The
release is created as a draft, whose assets are not publicly downloadable; the
`verify-and-publish` job re-downloads it, re-runs the whole documented path
(checksums, `cosign verify-blob`, `gh attestation verify`) against the real
published bytes, and only then promotes the draft. Publishing first and verifying
after was the obvious shape and is wrong: a release that fails verification would
already be installed, already marked latest, and there is no rollback -- the
workflow would go red while the artifact stayed up, which inverts the whole
point. Verification against the *published* bytes (rather than the dist we
uploaded from) is also what catches a create-release that uploaded only some of
its assets, and what keeps
[`docs/release-verification.md`](../release-verification.md) honest: if the
documented commands stop working, no release goes out.

The gate additionally runs *before* signing, because cosign will sign an
incomplete manifest without complaint, laundering a coverage gap into a valid
signature: `release/integrity.py` refuses to *build* a manifest over an
incomplete dist.

**SBOM scope follows the artifact.** The CLI binaries are cataloged from
`cli/Cargo.lock`, because the lockfile is what actually names every crate that
went in, stripping removes what a binary scan would need, and by SBOM time the
`cli` tree also holds a `target/` full of build detritus. The chart and the
compose file are deployment manifests with no dependencies of their own, so their
SBOMs inventory the packaged artifact itself; the dependency graph of what they
deploy belongs to the images they pin, which carry no SBOM or provenance today
(#62, open). This is the weakest part of the decision and is recorded as such: a
chart SBOM that names only the chart is close to a restatement of its checksum,
and a verified chart is not a verified stack until #62 lands.

## Consequences

- Verifying a release needs `cosign`, or `gh` for the one-command
  `gh attestation verify` path. Both are documented; neither is bundled.
- The verification snippets take the version as a `vX.Y.Z` placeholder the reader
  substitutes, rather than a live tag. The cosign certificate identity names the
  exact tag, so the snippet cannot be tag-agnostic; a wildcard identity would
  accept any tag's manifest and defeat the pin. A hardcoded tag would be worse
  than a placeholder -- it goes stale every release, and a stale tag sends people
  to a release whose integrity URLs 404.
- These artifacts exist only on releases published after v0.4.0. Verification is
  not retroactive: v0.4.0 and earlier shipped bare binaries and always will, so
  the docs say plainly that those cannot be verified rather than implying the
  commands work everywhere.
- A release binary still fetches the chart and compose assets itself at `cluster
  up` / `local up` and does **not** verify them; that fetch is protected by HTTPS
  to GitHub, not by this signature. Closing that is follow-on work -- the trust
  material now exists for the CLI to check, which it did not before.
- Apple signing and notarization remain deferred pending an Apple account
  decision. The macOS binary is covered by everything above; it is Gatekeeper,
  not verifiability, that is missing.
