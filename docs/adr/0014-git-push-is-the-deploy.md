# 14. A git push is the deploy: immutable bundles, promote-not-rebuild, evals as a CI gate

Date: 2026-07-09
Status: Accepted

Retroactive record of the git-flow deploy model already built into
[`apps/api/src/agentos_api/gitflow.py`](../../apps/api/src/agentos_api/gitflow.py).

## Context

An agent is a versioned artifact (a Claude Code plugin bundle) that has to move
from a developer's edit to a running bot with the same production discipline as
code: reproducible, testable before promotion, and identical in dev and prod.
The question was how a change becomes a deployment, and specifically what happens
to the artifact between environments.

## Decision

A git push is the deploy, and the artifact built for dev is the exact artifact
promoted to prod.

- A push is HMAC-verified (`gitflow.py:35`,
  [`routers/github.py:20`](../../apps/api/src/agentos_api/routers/github.py)).
- A **dev-branch** push clones and archives the SHA, runs the single
  `plugin_format.validate_bundle`, stores an **immutable versioned bundle**, and
  creates a `Version` + dev `Deployment` (`gitflow.py:136`,
  [`storage.py:22`](../../apps/api/src/agentos_api/storage.py)). It then fans out
  the bundle's eval suite as a CI check and reports pass/fail back as a GitHub
  commit status (`gitflow.py:186`,
  [`routers/evals.py:15`](../../apps/api/src/agentos_api/routers/evals.py),
  [`eval/stream.py:114`](../../apps/worker/src/agentos_worker/eval/stream.py)).
- A **prod-branch** push does not rebuild. It finds the already-built `Version`
  for that SHA and creates a prod `Deployment`, promoting the identical bytes
  (`gitflow.py:172`).
- The browser-authored path, `agentos local deploy`, and the webhook all
  terminate at the same `Version` / `Deployment` tables and the same validator, so
  there is one pipeline regardless of entry point.

## Alternatives considered

- **Rebuild the artifact on the prod push.** Rejected: a rebuild can differ from
  what dev tested (dependency drift, a moved base image), which breaks the one
  guarantee that makes the eval-as-CI gate meaningful, namely that prod runs the
  bytes the evals passed against. Promote-not-rebuild is the deliberate choice.
- **Mutable deployments / a moving `latest` bundle.** Rejected: a mutable bundle
  can change under a running deployment, so a version is no longer a reproducible
  fact. Immutability of the stored bundle is what lets a trace be tied back to an
  exact version (the eval and observability moat).
- **Evals as an optional or manual step.** Rejected: the production-discipline
  layer is the product's value; making the eval run a gate on the dev push (a
  commit status) is what turns "we have evals" into "a regression cannot silently
  promote."
- **A bespoke CD pipeline outside git.** Rejected: git branch to environment is
  the model developers already have in their hands; a separate deploy tool is more
  surface for no gain.

## Consequences

- Immutable-bundle storage and promote-not-rebuild are invariants: a contributor
  who rebuilds on prod, or introduces an overwrite path for a stored bundle,
  silently breaks dev/prod artifact identity.
- The env-switcher and promote-to-prod UI, and freezing the eval-case format, ride
  on this model ([#6](https://github.com/curie-eng/agentos/issues/6),
  [#8](https://github.com/curie-eng/agentos/issues/8)).
- The eval store itself is Langfuse (ADR-0004); this ADR is about the deploy flow
  and the gate, not where scores live.
