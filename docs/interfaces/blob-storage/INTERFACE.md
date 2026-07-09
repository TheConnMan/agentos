# INTERFACE: Blob storage (S3/MinIO)

> Part of the AgentOS swappable-seam catalog — see the [seam index](../../interfaces.md).
> **Kind:** SOFT &nbsp;·&nbsp; **Implementations today:** 2 &nbsp;·&nbsp; **Swap-readiness grade:** B+

**Kind legend:** CLEAN = a real `Protocol`/typed port class · SOFT = swap via env/URL/prefix/wire, no code interface · NONE = not built yet.

## The black line

Immutable plugin bundles are addressed by a deterministic `(agent, version)` key in
an S3-compatible object store. The swappable thing is the **backend behind the S3
wire protocol** (MinIO today, AWS S3 or any path-style S3 API tomorrow). What stays
opinionated core is the write-once/no-mutation key discipline and the bundle bytes
themselves. This is a deliberately un-abstracted seam: **the S3 protocol IS the port**
— there is no `StorageInterface` class, and one is extracted only when a non-S3
backend actually demands it (per the vision doc's "second implementation teaches the
interface" restraint).

## Current contract

A second implementation must speak the boto3 S3 API with **path-style addressing**,
configured entirely through env/settings — no code changes:

- **Env/settings** (`apps/api/src/agentos_api/config.py:36-40`): `s3_endpoint_url`,
  `s3_access_key`, `s3_secret_key`, `s3_region`, `bundle_bucket` (env vars
  `S3_ENDPOINT_URL`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_REGION`, `BUNDLE_BUCKET`).
- **Client construction** (`apps/api/src/agentos_api/storage.py:25-32`): `boto3.client("s3", endpoint_url=..., config=BotoConfig(s3={"addressing_style": "path"}))`.
- **Operations used** (`storage.py:39-66`): `head_bucket`, `create_bucket`,
  `head_object`, `put_object` (with `Body`, `ContentType`), `get_object` (reads
  `obj["Body"].read()`). The backend must support exactly these five calls, path-style.

## Implementations today

Two concrete client sites plus a third in the chart:

- **API writer** — `apps/api/src/agentos_api/storage.py:22` (`BundleStore`, async-offloaded boto3 write path).
- **Worker reader** — `apps/worker/src/agentos_worker/bundle_store.py:37` (`BundleStore`, read-only `get`, path-style, "same construction the API's write path uses").
- **Chart bundle-fetch init** — `charts/agentos/templates/agent-sandbox.yaml:110-111` uses the `mc` CLI (`mc alias set` / `mc cp`) rather than boto3, a third dialect of the same S3 protocol.

## Known leakage

The seam bleeds through **three hand-aligned client sites** that must agree on the
same endpoint/credentials/bucket by convention, not by a shared interface: the API's
boto3 writer, the worker's boto3 reader, and the chart's `mc`-based init container.
The worker's docstring even notes it mirrors the API "so the env names line up." A
switch to a fundamentally different backend (e.g. GCS-native rather than its S3
compatibility layer) would touch all three independently — that is the cost of not
abstracting, and the trigger that would justify extracting a real port.

## Cross-links

- **Epic(s):** #83 — vision epic for the blob-storage seam (extract a port only when a non-S3 backend lands).
- **Vision doc:** [architecture-vision.md](../../architecture-vision.md) — Job 4 (Blob storage), grade B+
- **ADR(s):** [ADR-0007](../../adr/0007-adopt-not-build-boundaries.md) — Adopt-not-build boundaries (MinIO adopted, AGPLv3, "offer BYO-S3")
