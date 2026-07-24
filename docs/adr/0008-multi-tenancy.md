# 8. Multi-tenancy: one code path, pooled RLS, hard-siloed compute

Date: 2026-07-09
Status: Accepted

## Context

Curie is single-tenant by construction today, which is correct for the
self-host case: one install is one company. The vision (grow the way Supabase and
PostHog did) also calls for a hosted offering where many customer companies share
our infrastructure. That is multi-tenancy, and it carries an absolute constraint:
nothing may ever cross a tenant boundary.

The data model is flat with no tenant key (`agents`/`agent_versions`/`deployments`,
`agents.name` and `repo_full_name` globally unique), auth is one shared API key
with no caller identity, and there is one of everything else (one Slack app, one
model credential, one MinIO bucket, one Langfuse project, one namespace). The
compute sandbox, by contrast, is already strong (ADR 0006): default-deny egress,
gVisor, non-root / read-only-rootfs, per-run budgets. So the gap is the control
plane, not the runtime isolation.

This ADR records the load-bearing decisions. The build path is tracked in epic
[#158](https://github.com/curie-eng/curie/issues/158) (stones #151-157); this is
the "why + when", not the roadmap. There is no prototype evidence behind it yet
(unlike the MVP-era ADRs); it is a forward decision, and if implementation proves
part of it wrong, a superseding ADR records the drift.

## Decision

1. **One code path; tenant count is 1..N; self-host is N=1.** No "single-tenant
   mode" and no fork. The tenant boundary is always on: a `tenant_id` is resolved
   at ingress and every query is scoped in every install. A self-host install
   auto-provisions one default tenant and never adds a second, which is simpler
   than today's implicit "one of everything" assumptions.

2. **Relational isolation: pooled Postgres + database-forced RLS. One database.**
   No schema-per-tenant, no database-per-tenant. The boundary is enforced by the
   engine, not by application query predicates: `FORCE ROW LEVEL SECURITY`
   policies (`USING (tenant_id = current_setting('app.tenant_id')::uuid)`), a
   non-superuser runtime role without `BYPASSRLS`, and `SET LOCAL app.tenant_id`
   per transaction. A CI gate fails if any tenant table lacks a policy.
   Database-per-tenant is parked as a possible future high-assurance tier.

3. **Compute stays hard-siloed: namespace-per-tenant.** Executing untrusted,
   prompt-injectable code that holds customer credentials is a different risk
   class than pooling rows, so sandboxes are isolated per tenant namespace with a
   per-agent ServiceAccount and secret-scoped Role (completing the per-agent RBAC
   that ADR 0006 deferred to the control plane).

4. **Other planes:** blob storage is prefix-per-tenant
   (`t/{tenant_id}/bundles/...`); Langfuse is project-per-tenant; every span is
   stamped with `tenant_id`. Model, Slack, and secret credentials move to a
   per-tenant (and per-agent/per-skill) secrets store; the flat chart Secret
   becomes the self-host default only.

5. **Slack ingress is a per-tenant OAuth app install, routed by `team_id`.**
   Socket Mode is one app per workspace, so it cannot be the multi-tenant path;
   "connect Slack in the UI" is the same feature as multi-tenant ingress and is
   wanted regardless.

6. **Control plane is API-first: the CLI is the complete, authoritative surface
   (verbs, not scripts); the UI wraps the same API and becomes primary over time;
   a self-serve signup page is far future.** Parity between the two surfaces is
   enforced by the `curie schema` manifest + CI gate (#145).

**The tenant-boundary invariant** (what we test and review to): every data
read/write, storage key, credential fetch, sandbox claim, and trace query is
scoped by a `tenant_id` resolved from the authenticated principal at ingress,
never from request body, path, or model output; no code path widens that scope.

## Consequences

- Self-host is unchanged and gets cleaner: implicit "one of everything" becomes
  one explicit `Tenant`. The same binary serves self-host (N=1) and hosted (N>1),
  differing only in additive surfaces (signup, billing, Slack OAuth) that
  self-host never turns on.
- Accepted residual risk of pooled density: the relational boundary is one
  logical layer. A superuser/migration connection, an accidental `BYPASSRLS`, or a
  table shipped without a policy is a cross-tenant leak. Mitigated by role
  hygiene, the missing-policy CI gate, and keeping compute and credentials
  hard-siloed so a relational slip is not also a credential slip.
- Slack ingress is the largest new-code area (OAuth install, per-tenant token
  storage, `team_id` routing); Socket Mode is retired for the hosted path.
- The build proceeds as a strangler fig: per-skill credentials and skill/channel
  decoupling ship first as single-tenant features that also lay tenancy
  groundwork. Sequencing and status live in epic #158.
