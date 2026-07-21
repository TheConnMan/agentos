# 65. Tag protection, not the workflow gate, binds a write actor

Date: 2026-07-21

Status: Accepted

**Amends [ADR-0058](0058-tag-push-is-not-release-authority.md) in part**
(back-link added under [ADR-0045](0045-the-status-line-is-the-mutable-part-of-an-immutable-adr.md)):
0058's Context claims "the workflow-level gate below is enforceable today
without that access," naming its `authorize-release` job as something a
write actor is already bound by. That claim is false against 0058's own
threat model, for the reason this ADR sets out below. The mechanism 0058
built -- the ancestry check, the required-checks check, and the
`release-publish` environment reference -- is unaffected and stands as an
accident-prevention control; only the claim that it constrains a determined
write actor is corrected here.

Addresses [#738](https://github.com/curie-eng/agentos/issues/738), introduced
by [#727](https://github.com/curie-eng/agentos/pull/727). Related: #628, #632,
#732, #733.

## Context

ADR-0058 names the threat precisely: "any repository write actor can create a
`v*` tag on any commit reachable from any branch, including one that never
went through review, and push it." Its remedy is `authorize-release`, a job
in `.github/workflows/release.yaml` that checks out the tagged commit and runs
`release/authorize.py` against it before any publishing job proceeds.

That remedy is read from the commit it is judging. On a tag push,
`actions/checkout` checks out the triggering ref -- the tagged commit -- so
`release/authorize.py` is loaded from that commit's tree, not from
`origin/main`. The workflow file itself is no different: GitHub Actions
resolves `push`-triggered workflow YAML from the pushed ref, so
`release.yaml` is also read from the tag.

Walk the threat ADR-0058 names through its own remedy. A write actor branches
off main, commits a change that makes `authorize()` in `release/authorize.py`
return unconditionally, or edits `release.yaml` to drop the `needs:
[authorize-release]` lines from `build`, `cli-binaries`, and `chart`
outright. Either edit needs no review: it is a commit on a private branch, not
a merge to `main`. The actor tags that commit `v9.9.9` and pushes. The
ancestry check in the tampered `authorize.py` -- if it still runs at all --
asks `git merge-base --is-ancestor <sha> origin/main` about the very commit
the actor just wrote, which is not reachable from `origin/main` and would
ordinarily fail closed. But the actor controls the function answering that
question, so it is not really being asked. The gate passes because the actor
told it to, and the full pipeline builds and pushes version-tagged images to
GHCR, builds CLI binaries, packages the chart, and cuts a signed GitHub
Release, none of it from reviewed code.

The ancestry check cannot rescue itself here: it is performed *by* the code
under judgment. A control that verifies a commit's provenance is only as
trustworthy as the copy of itself doing the verifying, and on a tag push that
copy is supplied by the tag.

0058 was not wrong that this closes real gaps. Before it, a tag pushed by
accident -- the wrong commit, a tag cut before CI finished, a rebase that
silently moved what a branch pointed at -- sailed straight through to
publication with no check at all. `authorize-release` stops every one of
those, because none of them involve an actor rewriting the gate. What 0058
overstated is the boundary of that protection: it filed the two admin-only
controls it could not configure -- a `v*` tag protection rule, required
reviewers on the `release-publish` environment -- under "harden it further,"
language that reads as an upgrade to an already-sound gate. Against a write
actor who intends to bypass the gate rather than stumble into a mistake, tag
protection is not a hardening pass over a working control. It is the only
control in this design a write actor cannot simply rewrite, because it is
enforced by GitHub's ref-update authorization before any workflow file is
ever read, not by anything checked out alongside the code being judged.
Required reviewers on `release-publish` are the second such control, for the
same reason: an environment protection rule lives in repository settings, not
in a file the tagged commit carries.

## Decision

**Restate the trust boundary plainly.** `authorize-release`'s ancestry and
required-checks logic is an accident-prevention control. It catches a tag
pushed at the wrong commit or ahead of green CI -- the ordinary-mistake case
-- and it does this well. It does not bind a write actor who edits
`release/authorize.py` or `release.yaml` on the same branch before tagging,
because both files are read from the ref under judgment. The only controls
that bind such an actor are enforced by GitHub outside any file the tagged
commit carries:

- **A `v*` tag protection rule** (Settings -> Tags), so creating or force-
  pushing a tag matching `v*` itself requires elevated permission the actor's
  ordinary write access does not grant. This is the load-bearing control: it
  stops the attack at the one step -- the tag push -- that no in-repo file can
  intercept, because GitHub evaluates it before checkout, before the workflow
  file loads, before anything in `release/` runs.
- **Required reviewers on the `release-publish` environment** (Settings ->
  Environments), so even a tag push that clears ref protection still pauses
  the pipeline for a named human's sign-off before `authorize-release` runs.
  This is the second, independent binding control, and it does not depend on
  the workflow file being honest either -- it is GitHub deciding whether the
  job is allowed to start.

Neither is configured today. Both are admin-only repository settings; this
account holds `push` only (confirmed via `gh api repos/curie-eng/agentos
--jq .permissions`), not `admin` or `maintain`, so neither can be turned on
from this session, the same wall #628 and #632 already recorded hitting.
That is not a decision being deferred quietly -- it is the actual state of the
one control this ADR concludes is load-bearing, and it is recorded in
Consequences below as blocking, not as a footnote.

**Reduce, without eliminating, the self-reference.** `authorize-release`'s
checkout step is changed to resolve and check out `origin/main`'s copy of
`release/` specifically, rather than trusting the tagged ref's own copy, so
`release/authorize.py` itself is no longer attacker-supplied on a tampered
tag. This closes exactly one edge of the gap: an actor who only edits
`release/authorize.py` on their branch before tagging no longer gets a
cooperative copy of the script. It does not close the other edge. The
workflow file that invokes the script -- `release.yaml` -- is still resolved
by GitHub Actions from the pushed tag's ref on a `push` trigger; that is a
platform behavior, not something a checkout step inside the workflow can
redirect, since the workflow has to already be loaded and running before any
of its steps execute. An actor who instead edits `release.yaml` to drop the
`needs: [authorize-release]` guards is unaffected by which copy of
`authorize.py` exists, because their tampered workflow file never calls it.
This ADR does not claim otherwise; the residual gap is recorded as open in
Consequences.

## Consequences

- **Tag protection on `v*` is now the priority, not a follow-up.** It blocks
  the goal ADR-0058 itself was implementing (#628 criterion 3, "pushing or
  deleting a matching tag itself requires elevated permission") and #632's
  admin-gated items are queued behind the identical access wall. Both should
  be resolved in one deliberate admin session by whoever holds `admin` or
  `maintain` on `curie-eng/agentos`, rather than continuing to accumulate as
  separately-filed footnotes across issues.
- **Required reviewers on `release-publish` remain unset.** Until a maintainer
  adds them, the `environment:` line on `authorize-release` is inert, exactly
  as ADR-0058 already recorded; this ADR does not change that fact, only
  where it is filed -- as a binding control blocked on admin access, not a
  hardening option.
- **The ancestry/checks gate is unaffected and keeps its value.** It still
  refuses an accidentally-tagged commit that is not on `origin/main` or whose
  checks are not green, exactly as before. Nothing in this ADR removes or
  weakens that behavior; the correction is to what threat it was ever
  credited with stopping.
- **`release/` is now sourced from `origin/main` in `authorize-release`**,
  narrowing the self-reference to the one file GitHub Actions itself always
  reads from the tagged ref (`release.yaml`). A tampered `authorize.py` on a
  malicious branch no longer runs; a `release.yaml` with the `needs:` guards
  stripped still bypasses everything downstream, because that tampering never
  reaches the trusted script at all. This is recorded as a partial mitigation,
  not a closed hole.
- **This account cannot verify either binding control by demonstration.**
  Unlike ADR-0058's ancestry/checks logic, which the pytest suite exercises
  against constructed fixtures, a tag protection rule and an environment's
  required reviewers are repository settings with no code path to assert
  against from here. The verification that they were actually configured has
  to happen in GitHub's Settings UI or API by whoever holds the access, and
  that verification step is itself the remaining acceptance criterion this
  ADR leaves open.

## Alternatives considered

- **Edit ADR-0058 directly to soften the claim.** Rejected per ADR-0001 as
  amended by ADR-0045: ADRs are immutable once Accepted outside their
  `Status:` line and a back-link. Softening the Context's prose in place
  would erase the record that the claim was made and later found wrong, which
  is the failure ADR-0045 exists to prevent.
- **Attempt to configure the tag protection rule or the environment's
  required reviewers from this session.** Not possible: both are gated on
  `admin`/`maintain`, which this account's `push`-only permission does not
  grant, verified against the live API rather than assumed.
- **Skip the `origin/main` checkout change and only restate the trust
  boundary in prose.** Considered, since the issue marks it optional and it
  does not close the hole on its own. Implemented anyway because it is a
  small, additive change to a checkout step -- it does not touch
  `release/authorize.py`'s logic -- and it removes one real edge of the
  self-reference (a tampered `authorize.py`) even though the other (a
  tampered `release.yaml`) is left open and documented as such.
