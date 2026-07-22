# 70. A bundle-local `.env` is an opt-in, lowest-priority credential source

Date: 2026-07-21

Status: Accepted

Sits alongside [ADR-0015](0015-credential-plane.md) (the credential plane) and
reuses the fail-closed, forward-by-name model-credential path
(`cli/src/commands.rs::select_passthrough_env`, frozen as data in
`cli/tests/vectors/model-credential-forwarding.json`, ADR-referenced by #495)
without changing it. Supersedes nothing.

Closes the first half of [#749](https://github.com/curie-eng/agentos/issues/749)
("credential and Slack wiring is manual shell plumbing"): the model-credential
side. Persisting Slack tokens for the local worker, and extending pickup to the
compose-tier (`local up`) credential path and to `--secret` connector tokens,
are deliberately deferred as follow-ups (see Consequences).

## Context

To run a bundle live, the model credential must be exported into the invoking
shell before `agentos skill up` / `agentos local up`. AgentOS reads
`ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` / `AGENTOS_CREDENTIALS` from the
process env plus its own secret vault (`agentos secrets set`), but it does **not**
read a bundle's own `.env` — even though that file already holds the key (the
older `make serve` flow auto-loaded it). So the operator runs
`set -a; source .env; set +a` before `skill up`, and repeats it after every
`skill down`. Our cold-start prompt even pre-teaches this workaround as a
landmine, which is why our own runs stopped surfacing it as a defect — patching
the prompt instead of the product.

The intent to fix this was already half-present: `dotenvy` is declared in
`cli/Cargo.toml` but has zero `dotenvy::` call sites — a dead dependency.

Two constraints shape the fix:

1. **Fail-closed posture.** Silently absorbing a dotfile into a process (and
   thence a child Docker) environment is exactly the kind of implicit
   credential flow the credential plane avoids. Reading a `.env` must be an
   explicit gesture, and it must never pull arbitrary keys from the file into
   any process env — only the recognized credential names.
2. **A precedence order must be decided once and stated, not discovered.** The
   same credential can come from three places; which wins has to be a recorded
   decision so a future contributor does not re-derive it per call site.

## Decision

**Precedence, highest wins: shell env > vault (`secrets set`) > bundle `.env`.**

- An explicitly-exported shell env var is the most immediate, intentional
  override and wins — this preserves today's behavior exactly.
- A deliberately-stored vault secret is next.
- The bundle `.env` is a last-resort convenience and is consulted only for a
  credential name that is absent from **both** the shell env and the vault.

**Opt-in via `agentos skill up --env-file <PATH>`.** No path, no `.env` read —
the flag is the explicit gesture. The mechanism reuses the existing
`docker_env` fallback lane: recognized credential values parsed from the file
are appended *after* the vault-hydrated entries and only for names not already
supplied, so `select_passthrough_env` — the frozen authority on what is
actually forwarded — is untouched. Only the recognized credential names
(`AGENTOS_CREDENTIALS`, `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`) are read
from the file; every other key in the dotfile is ignored, never injected. This
retires the dead `dotenvy` dependency.

**Scope: `skill up` first.** The skill tier's single-runner-container credential
path (`commands.rs::start`) is the primary "boot live with no source step"
onboarding surface and has the clean, verified insertion point above. The
compose-tier path (`local up`, `local.rs`/`ops.rs::resolve_up_credentials`) is a
separate mechanism and is deferred so this security-sensitive change stays
surgical.

## Consequences

- `agentos skill up --env-file .env` boots live with no `source` step, and the
  credential still never appears in argv (forwarded by name; the value reaches
  only the Docker CLI child so it can copy it into the container).
- The precedence is now stated and enforced at one site, not rediscovered.
- The dead `dotenvy` dependency becomes load-bearing.
- **Deferred, tracked under #749:** (a) the same opt-in pickup for the
  compose-tier `local up` credential path; (b) applying it to `--secret`
  connector tokens; (c) persisting local-worker Slack tokens in the vault so
  `local comms --slack` needs no re-export (the cluster path already persists
  via Helm). Each is its own change; the precedence decided here applies to all
  of them.
