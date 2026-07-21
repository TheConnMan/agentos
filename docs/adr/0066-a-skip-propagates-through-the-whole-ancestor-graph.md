# 66. A skip propagates through the whole ancestor graph, so every publishing job carries its own guard

Date: 2026-07-21

Status: Accepted

**Amends [ADR-0058](0058-tag-push-is-not-release-authority.md) in part**
(back-link added under [ADR-0045](0045-the-status-line-is-the-mutable-part-of-an-immutable-adr.md)):
0058's Decision states that "every job downstream of `build` (`merge`,
`worker-local-build`, `worker-local-merge`) already depended on it and so is
covered without further edits", and its Consequences state that a push straight
to `main` is "unaffected". Both are false as built, for the reason set out
below. The gate 0058 built (ancestry, required checks, the `release-publish`
environment) is unaffected and stands exactly as decided; only the claim that an
`always()` on `build` alone is enough to keep the continuous image path running
is corrected here.

Fixes [#787](https://github.com/curie-eng/agentos/issues/787).

## Context

ADR-0058 added `authorize-release` as the gate the tag-triggered publish
pipeline hangs off, and gave `build` an `always()`-based `if:` so that a
*skipped* gate (an ordinary push to `main`, where there is nothing to authorize)
still lets the continuous image path run, while a *failed* gate stops it.

That reasoning is right for `build` and wrong for everything under it. GitHub
Actions does not decide a job's skip from its immediate `needs:` edge alone: a
skipped job skips every job that depends on it directly **or transitively**, and
an intermediate job opting itself back in with `always()` does not clear the
skip for its own dependents. `merge` declared only `needs: [build]` with no
`if:`, so on every push to `main` after the gate landed, `build` reported
success on all ten matrix legs and all six merge jobs were skipped anyway, along
with `worker-local-build` and `worker-local-merge` beneath them.

The failure was silent in the worst way. Each `build` leg pushes its image *by
digest only*; the tags come from `merge`, which assembles the multi-arch
manifest and applies `sha-<sha>` and `latest`. With `merge` skipped, every main
push published unreferenced digests and no tags, so
`ghcr.io/curie-eng/agentos-*:latest` sat frozen at the last tag push while the
workflow stayed green. Run 29843949137 on main is a concrete instance: ten
successful builds, six skipped merges.

## Decision

**Every job that the ancestor graph would skip names a status function and
checks its dependency's result explicitly.** `merge`, `worker-local-build`, and
`worker-local-merge` each gain:

```yaml
if: always() && needs.<dependency>.result == 'success'
```

The `always()` is what opts the job out of ancestor-graph skip propagation; it
is never sufficient on its own, and a bare `always()` would be a regression
rather than a fix, because it would run a manifest merge after a failed or
cancelled build. The explicit `== 'success'` restores exactly the strictness the
implicit `success()` had, so the chain still stops on a failure, on a
cancellation, and on the skip that a refused `authorize-release` induces in
`build`.

**The gate's guarantee is unchanged and is what the result check preserves.** On
a tag push whose `authorize-release` fails, `build`'s condition is false, so
`build` is skipped; `merge` then sees `needs.build.result == 'skipped'` and does
not run, and the same reasoning carries down the overlay chain. `cli-binaries`,
`chart`, `release`, and `verify-and-publish` are untouched: their conditions
contain no status function, so they keep both the implicit `success()` and the
ancestor-graph skip, which is the behavior wanted there.

## Consequences

- The continuous path publishes again: a push to `main` builds ten single-arch
  images, merges five manifests tagged `sha-<sha>` and `latest`, then builds and
  merges the `worker-local` overlay.
- "Downstream of a gated job" is no longer a safe assumption anywhere in this
  repo's workflows. A job inheriting a guard through an intermediate that uses
  `always()` inherits nothing; the guard has to be restated at each hop. That is
  three extra lines per job and it is the price of the pattern.
- The four cases are now enumerable from the file itself. Plain main push:
  `authorize-release` skipped, everything on the image path runs. Authorized tag
  push: the gate succeeds and the whole pipeline runs. Tag push with a failed
  gate: `build` skipped, and every publishing job below it declines on the
  `skipped` result, so nothing reaches GHCR or a Release. Failed build: `merge`
  declines on the `failure` result and no manifest is tagged.
- This bug shipped green for the same reason the gate's own success would have:
  a skipped job is not a red workflow. Nothing in CI asserts that a main push
  actually tagged an image, and this ADR does not add such an assertion. The
  detection path remains a human noticing that `latest` is stale, which is how
  #787 was found.

## Alternatives considered

- **Bare `always()` on `merge`.** Rejected: it decouples the manifest merge from
  the build result entirely, so a failed or cancelled `build` would still have
  its partial digests assembled and tagged, publishing a manifest missing an
  architecture.
- **Give `merge` `needs: [build, authorize-release]` and repeat `build`'s
  condition.** Rejected as redundant. `build` already encodes the gate's verdict
  in its own result, so checking `build` alone is both sufficient and the single
  place that verdict is interpreted. Restating the authorize condition at four
  sites is four places for it to drift.
- **Drop `authorize-release` from the graph and gate inside each job.** Rejected:
  it discards the property ADR-0058 exists for. A gate that runs inside the
  publishing job is a gate the publishing job's own steps can be reordered
  around, and the `environment: release-publish` hook only pauses a job, so it
  has to sit on a job that runs first and does nothing else.
