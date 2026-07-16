# ADR-0047: Canonical env-var names for the API base URL and model credential

Status: Accepted
Date: 2026-07-16

## Context

Two configuration concepts were each read under two different env-var names
across the codebase, so an operator who set one name got a silent no-op where the
other was expected, and the docs already contradicted each other (#496):

- **Platform API base URL** — the CLI and the platform API read `AGENTOS_API_URL`;
  the worker and dispatcher (and the chart/compose that wire them) read
  `AGENTOS_API_BASE_URL`.
- **Model credential** — the CLI's `cluster up` install flag read
  `AGENTOS_MODEL_CREDENTIALS` (and the chart notes named it); the entire runtime
  plane (runner, worker, chart `secretKeyRef`, `reserved_env`) reads
  `AGENTOS_CREDENTIALS`.

The worker and dispatcher additionally hand-mirrored the same six shared env
names (`AGENTOS_API_*`, `AGENTOS_STREAM`, the heartbeat pair, `AGENTOS_SHIMMER`)
plus an identical copy of a custom `_AliasOnlyEnvSource`, so a rename could drift
one service out of sync with the other.

## Decision

**Canonical names:** `AGENTOS_API_URL` (base URL) and `AGENTOS_CREDENTIALS`
(model credential) — chosen as the names already used by the most surfaces, so
the runtime credential plane and the chart `secretKeyRef` are untouched.

**One-release deprecation:** the historical twin (`AGENTOS_API_BASE_URL`,
`AGENTOS_MODEL_CREDENTIALS`) keeps working for one release and logs a deprecation
warning naming the replacement.

- The **services** accept the URL under `AliasChoices("AGENTOS_API_URL",
  "AGENTOS_API_BASE_URL")` (canonical first → wins when both are set) and log a
  warning when only the deprecated name is present.
- The **CLI** reads `AGENTOS_CREDENTIALS` first, falling back to
  `AGENTOS_MODEL_CREDENTIALS` with a warning.

**Shared declaration:** the six shared env-var names and the `AliasOnlyEnvSource`
are declared once in `aci_protocol.service_config` (a package both the worker and
dispatcher already depend on) and imported by both, so they can never drift apart
again.

**Emit the canonical name now:** the chart (`_helpers.tpl`) and compose files are
flipped to emit `AGENTOS_API_URL` in this same change (with their assertions/tests
updated in lockstep), so the platform never triggers its own deprecation warning.
The deprecated alias exists only for external/hand-written configs during the
window.

## Consequences

- An operator who set either name for one tier no longer gets a silent no-op on
  another; the deprecated name warns instead of failing silently.
- The worker/dispatcher config duplication (names + `AliasOnlyEnvSource`) is
  gone; a future rename is one edit in the shared module.
- A follow-up change removes the deprecated aliases and their warnings after the
  one-release window, at which point `AGENTOS_API_BASE_URL` /
  `AGENTOS_MODEL_CREDENTIALS` stop resolving.
