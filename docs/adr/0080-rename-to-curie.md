# 80. Rename the project to Curie

Date: 2026-07-24
Status: Accepted

Records the project-wide rename from its original name, AgentOS, to **Curie**,
and pins the decisions that the mechanical rename could not settle on its own:
which identifiers move to the company-owned domain, how env vars and data-plane
names behave across the cut, and why two ADR filenames keep the old name. The
name change aligns the project with the company brand (CurieTech AI,
`curietech.ai`); "Relay" remains the internal engineering codename, unchanged.

## Context

The project shipped through v0.4.x under the name AgentOS: the CLI binary was
`agentos`, module and package names were `agentos-*`, env vars were `AGENTOS_*`,
and various identifiers pointed at `agentos.dev`. Company branding settled on
CurieTech AI with the single owned domain `curietech.ai`, and the product
surface (CLI binary, bot handle, console) is `curie`. The names had diverged:
the product was already "Curie" to users while the codebase still said AgentOS
throughout.

A single mechanical sweep handled the bulk substitution
(`agentos`→`curie`, `AgentOS`→`Curie`, `AGENTOS`→`CURIE`,
`Curie Engineering`→`CurieTech AI`) across content, and renamed the module
directories and the Helm chart. That sweep is a **branding substitution, not a
decision change**: no ADR's reasoning or alternatives were altered in substance.
The doclint citation gate is what forces ADR *content* to be updated when a
referenced path is renamed, so swept text inside older ADRs is a mechanical
consequence of keeping citations valid, not a re-litigation of those decisions.

A short list of identifiers could not be settled by regex and is decided here.

## Decision

1. **The project is named Curie; the CLI binary is `curie`.** This is the
   product-surface name aligned with CurieTech AI. "Relay" stays as the internal
   codename (repo, commits, internal docs); both names refer to the same system.

2. **Identifiers move onto the owned domain `curietech.ai`.** The company owns
   only `curietech.ai`, so every identifier that embedded an unowned or wrong
   domain is repointed there:
   - Protocol schema `$id`s become `https://curietech.ai/schemas/<name>.schema.json`
     (correcting a pre-existing `curie.tech` typo — a domain the company does
     not own).
   - Versioned CLI/runner schema `$id`s move to the `schemas.curietech.ai`
     subdomain, preserving the ADR-0074 version-in-URL contract.
   - The Kubernetes label/annotation prefix becomes `curietech.ai/` (e.g.
     `curietech.ai/managed-by`), replacing the swept-but-still-unowned
     `curie.dev`.
   - The OS keyring service string becomes `ai.curietech.curie`.

3. **Env vars are `CURIE_*` with no back-compat aliases.** The old `AGENTOS_*`
   names are dropped outright, not shimmed. A closed-world boot-env contract
   already gates unknown names, so a lingering alias would be dead surface;
   a clean break is the honest state.

4. **Clean break on data-plane names.** Valkey stream keys, the Postgres schema,
   and Langfuse seed identifiers adopt the `curie` names with no dual-write or
   migration bridge. These are internal to a self-hosted install and are
   recreated on deploy, so there is nothing to migrate across the cut.

5. **ADR filenames `0021-agentos-...` and `0073-agentos-...` keep the old name.**
   Their filenames are stable link anchors that inbound references resolve
   against; renaming them would break those links for no gain. The rename's
   verification asserted no tracked path contains `agentos` except these two
   filenames. Their
   heading and body text are swept to Curie like every other ADR; only the
   filename is frozen.

6. **Old artifacts are left untouched.** Released binaries, published packages,
   git history, and already-cut tags keep their AgentOS names. The rename is
   forward-looking: the first Curie-named release is **v0.5.0**.

7. **No "formerly AgentOS" breadcrumb.** There is no "previously known as"
   trailer in the README, docs, CLI copy, or UI. The clean cut is deliberate;
   the only trail left is the GitHub repository redirect (the repo is renamed to
   `curie-eng/curie`, and GitHub redirects the old path). This ADR is the single
   intentional exception: naming AgentOS here is the historical record, which is
   what an ADR is for.

## Consequences

- The rename is a hard cut. An operator upgrading across it re-deploys with
  `CURIE_*` env vars and the new chart name; there is no in-place alias path,
  by decision 3.
- Identifiers now resolve (or could resolve) under a domain the company
  actually owns, closing the `curie.tech` typo and the unowned-`curie.dev`
  exposure in one move.
- The two frozen ADR filenames are a permanent, documented exception to the
  no-`agentos`-in-paths gate; a future contributor reading a broken-looking
  filename finds the reason here.
- Anyone bisecting or reading history before this commit sees AgentOS names,
  which is correct: they are the real names of those older artifacts.
