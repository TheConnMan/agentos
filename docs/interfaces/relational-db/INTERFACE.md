---
seam: Relational DB (Postgres)
kind: SOFT
impls: "1"
grade: A-
epics:
  - "#84"
order: 10
---

# INTERFACE: Relational DB (Postgres)

> Part of the AgentOS swappable-seam catalog — see the [seam index](../../interfaces.md).

<!-- BEGIN GENERATED: header (agentos dev docs-lint) -->
> **Kind:** SOFT &nbsp;·&nbsp; **Implementations today:** 1 &nbsp;·&nbsp; **Swap-readiness grade:** A-
<!-- END GENERATED: header -->

**Kind legend:** CLEAN = a real `Protocol`/typed port class · SOFT = swap via env/URL/prefix/wire, no code interface · NONE = not built yet.

## The black line

App state (agents, versions, deployments) lives in a Postgres database, reached
through SQLAlchemy 2.0 (async) with Alembic migrations. The swappable thing is the
**Postgres instance behind the DSN** — compose Postgres, RDS, Cloud SQL, any managed
Postgres — while the SQL/ORM layer and the `agentos` schema stay opinionated core.
This is a deliberately un-abstracted seam: a managed-Postgres swap is a **DSN change**
(`DATABASE_URL`), not a code change. There is no repository/DAL port; SQLAlchemy 2.0 +
Alembic *is* the contract, extracted into something narrower only if a non-Postgres
store is ever demanded.

## Current contract

A second implementation must be a Postgres speaking the async `asyncpg` dialect and
honoring the models/migrations verbatim:

- **DSN + schema** (`apps/api/src/agentos_api/db.py::SCHEMA`, `apps/api/src/agentos_api/db.py::create_engine`): `SCHEMA = get_settings().db_schema` (default `"agentos"`, `apps/api/src/agentos_api/config.py::Settings`); the engine is built from `database_url` (env `DATABASE_URL`) via `create_async_engine(..., pool_pre_ping=True)`.
- **Schema-scoped metadata** (`apps/api/src/agentos_api/db.py::Base`): `Base.metadata = MetaData(schema=SCHEMA)` — every table is qualified into the `agentos` schema.
- **Models** (`apps/api/src/agentos_api/models.py`): `apps/api/src/agentos_api/models.py::Agent` (table `agents`), `apps/api/src/agentos_api/models.py::AgentVersion` (table `agent_versions`), `apps/api/src/agentos_api/models.py::Deployment` (table `deployments`), plus the `apps/api/src/agentos_api/models.py::Environment` StrEnum.
- **Migrations**: 5 Alembic revisions in `apps/api/alembic/versions/` (`0001_initial` → `0005_behavior_packs`); the target DB must apply this chain.

## Implementations today

One: the compose/dev Postgres, reached through the single SQLAlchemy async engine in
`apps/api/src/agentos_api/db.py::create_engine`. Tests point the same engine at the compose
Postgres by overriding `database_url` (per the module docstring).

## Known leakage

Two Postgres-isms make the "just change the DSN" story leak for a non-Postgres store:

1. **`postgresql.UUID` column type** — `apps/api/src/agentos_api/models.py::UUID` is imported
   from `sqlalchemy.dialects.postgresql` and used as `UUID(as_uuid=True)` on every primary
   and foreign key (e.g. `apps/api/src/agentos_api/models.py::Agent`). This is a dialect-specific type.
2. **Schema-qualified tables + a schema-scoped native enum** — foreign keys are
   written as `f"{SCHEMA}.agents.id"` (`apps/api/src/agentos_api/models.py::AgentVersion`,
   `apps/api/src/agentos_api/models.py::Deployment`) and the `environment`
   column is a native Postgres `Enum(Environment, name="environment", schema=SCHEMA)`
   (`apps/api/src/agentos_api/models.py::Deployment`), which materializes as a `CREATE TYPE` in the `agentos` schema.

Both are cheap within the Postgres family (the A- grade) but would need rework for a
different RDBMS — the marker that a real port should be extracted first.

## Cross-links

- **Swap guide + validation:** [managed-postgres-swap.md](./managed-postgres-swap.md) — the DSN-only swap to RDS/Cloud SQL/Neon, with the `apps/api/tests/test_managed_pg_swap.py` smoke test that proves it (#283).
- **Epic(s):** #84 — vision epic for the relational-DB seam (keep the swap a DSN change; extract a port only for a non-Postgres store).
- **Vision doc:** [architecture-vision.md](../../architecture-vision.md) — Job 5 (Relational database), grade A-
- **ADR(s):** [ADR-0007](../../adr/0007-adopt-not-build-boundaries.md) — Adopt-not-build boundaries ("vanilla Postgres" adopted for app state)
