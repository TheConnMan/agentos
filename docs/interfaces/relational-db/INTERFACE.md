# INTERFACE: Relational DB (Postgres)

> Part of the AgentOS swappable-seam catalog — see the [seam index](../../interfaces.md).
> **Kind:** SOFT &nbsp;·&nbsp; **Implementations today:** 1 &nbsp;·&nbsp; **Swap-readiness grade:** A-

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

- **DSN + schema** (`apps/api/src/agentos_api/db.py:21,29`): `SCHEMA = get_settings().db_schema` (default `"agentos"`, `config.py:21`); the engine is built from `database_url` (env `DATABASE_URL`, `config.py:18`) via `create_async_engine(..., pool_pre_ping=True)`.
- **Schema-scoped metadata** (`db.py:24-25`): `Base.metadata = MetaData(schema=SCHEMA)` — every table is qualified into the `agentos` schema.
- **Models** (`apps/api/src/agentos_api/models.py`): `Agent` (`agents`, line 25), `AgentVersion` (`agent_versions`, line 58), `Deployment` (`deployments`, line 79), plus the `Environment` StrEnum (`models.py:20`).
- **Migrations**: 5 Alembic revisions in `apps/api/alembic/versions/` (`0001_initial` → `0005_behavior_packs`); the target DB must apply this chain.

## Implementations today

One: the compose/dev Postgres, reached through the single SQLAlchemy async engine in
`apps/api/src/agentos_api/db.py:28-33`. Tests point the same engine at the compose
Postgres by overriding `database_url` (per the module docstring).

## Known leakage

Two Postgres-isms make the "just change the DSN" story leak for a non-Postgres store:

1. **`postgresql.UUID` column type** — `models.py:14` imports `UUID` from
   `sqlalchemy.dialects.postgresql`, used as `UUID(as_uuid=True)` on every primary
   and foreign key (e.g. `models.py:29, 62, 82`). This is a dialect-specific type.
2. **Schema-qualified tables + a schema-scoped native enum** — foreign keys are
   written as `f"{SCHEMA}.agents.id"` (`models.py:65, 88-89`) and the `environment`
   column is a native Postgres `Enum(Environment, name="environment", schema=SCHEMA)`
   (`models.py:91-92`), which materializes as a `CREATE TYPE` in the `agentos` schema.

Both are cheap within the Postgres family (the A- grade) but would need rework for a
different RDBMS — the marker that a real port should be extracted first.

## Cross-links

- **Swap guide + validation:** [managed-postgres-swap.md](./managed-postgres-swap.md) — the DSN-only swap to RDS/Cloud SQL/Neon, with the `apps/api/tests/test_managed_pg_swap.py` smoke test that proves it (#283).
- **Epic(s):** #84 — vision epic for the relational-DB seam (keep the swap a DSN change; extract a port only for a non-Postgres store).
- **Vision doc:** [architecture-vision.md](../../architecture-vision.md) — Job 5 (Relational database), grade A-
- **ADR(s):** [ADR-0007](../../adr/0007-adopt-not-build-boundaries.md) — Adopt-not-build boundaries ("vanilla Postgres" adopted for app state)
