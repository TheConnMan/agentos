# Guide: swapping to managed Postgres (RDS / Cloud SQL / Neon)

> Companion to [INTERFACE.md](./INTERFACE.md). Part of the #84 relational-DB seam
> epic; validates the concrete swap tracked by #283.

Pointing AgentOS at a managed Postgres is a **connection-string change**, not a code
change. The SQL/ORM layer (SQLAlchemy 2.0 async + Alembic) and the `agentos` schema
stay put; only the Postgres instance behind the DSN moves. This guide shows the swap
and the one validation that proves it.

## The swap

1. **Provision a Postgres** on your managed provider (RDS, Cloud SQL, Neon, or any
   standard Postgres). No extensions are required. AgentOS confines its tables to a
   dedicated `agentos` schema (`config.db_schema`), so it can share a database with
   Langfuse without colliding with Langfuse's `public`-schema Prisma baseline.

2. **Point `DATABASE_URL` at it.** The engine is built from this env var alone
   (`apps/api/src/agentos_api/config.py`, `apps/api/src/agentos_api/db.py`). Use the
   async `asyncpg` dialect:

   ```
   DATABASE_URL=postgresql+asyncpg://<user>:<password>@<host>:5432/<database>
   ```

   - **RDS / Cloud SQL:** the endpoint host + port; TLS is negotiated by asyncpg.
     For Cloud SQL prefer the Auth Proxy (a localhost endpoint) so the DSN stays a
     plain host/port.
   - **Neon:** use the pooled or direct endpoint host; keep `sslmode=require`
     semantics by connecting over the provider's TLS endpoint.
   - The role must own (or be able to create) the `agentos` schema. Migration
     `0001_initial` issues `CREATE SCHEMA IF NOT EXISTS agentos`.

3. **Apply migrations** against the target:

   ```bash
   cd apps/api
   DATABASE_URL=<managed-dsn> uv run alembic upgrade head
   ```

   That is the whole swap. The models and the migration chain are applied verbatim.

## Why it just works (and where it would leak)

Two Postgres-isms are the only things that make the "just change the DSN" story
leak — and only for a *non*-Postgres store. Both are cheap within the Postgres
family, so any managed Postgres is unaffected:

1. **`postgresql.UUID` column type** — every primary/foreign key. Native `uuid` on
   any Postgres.
2. **Schema-qualified tables + a schema-scoped native enum** — the `environment`
   column is a native `CREATE TYPE ... environment` in the `agentos` schema. Created
   from the same migration on any Postgres.

Cross-database portability (MySQL, etc.) is explicitly a non-goal
([ADR-0007](../../adr/0007-adopt-not-build-boundaries.md)); if a non-Postgres store
is ever demanded, extract a repository port first.

## Validation (smoke test)

`apps/api/tests/test_managed_pg_swap.py` is the executable proof of this swap. The
test suite's `migrated` fixture provisions a throwaway database reached **purely by
overriding `DATABASE_URL`** and runs `alembic upgrade head` against it — the exact
shape of pointing the app at a managed Postgres. The test then asserts the schema
came up cleanly and that both Postgres-isms materialized as expected (native `uuid`
columns; the `environment` enum type in the `agentos` schema with the right labels;
zero tables in `public`).

```bash
cd apps/api
uv run pytest tests/test_managed_pg_swap.py -q   # needs a reachable Postgres (compose is fine)
```

A green run against the compose Postgres is a green run against RDS/Cloud SQL/Neon:
same dialect, same models, same migrations — only the DSN differs.
