# INTERFACE: Blob storage (S3/MinIO)

> Part of the AgentOS swappable-seam catalog — see the [seam index](../../interfaces.md).
> **Kind:** CLEAN &nbsp;·&nbsp; **Implementations today:** 1 backend (S3/MinIO) behind the `ObjectStore` port &nbsp;·&nbsp; **Swap-readiness grade:** A-

**Kind legend:** CLEAN = a real `Protocol`/typed port class · SOFT = swap via env/URL/prefix/wire, no code interface · NONE = not built yet.

## The black line

Immutable plugin bundles are addressed by a deterministic `(agent, version)` key in
an object store, behind the **`ObjectStore` port** (`apps/api/.../storage.py`,
#282 / ADR-0026): `ensure_bucket` / `exists` / `put` / `get`, with the
write-once/no-mutation key discipline promoted from convention **into the port's
contract**. The one backing today is S3/MinIO (`BundleStore`); a future non-S3
backend (GCS-native, Azure Blob) is a drop-in that satisfies the Protocol. The
GCS/Azure adapter itself is deliberately **not built** — it stays gated on a real
non-S3 customer (ADR-0007), so only the *port* is extracted now, not a speculative
second implementation.

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

One backend (S3/MinIO) behind the port, plus the chart's `mc` init:

- **`ObjectStore` port** — `apps/api/src/agentos_api/storage.py` (`Protocol`: the
  five ops + the write-once contract). Consumers (`deps`/`gitflow`/`deploy`) type
  against it, so a second backend is a drop-in.
- **API writer** — `apps/api/.../storage.py` `BundleStore`, the S3/MinIO backing
  (async-offloaded boto3); client built by the shared `build_s3_client` factory.
- **Worker reader** — `apps/worker/.../bundle_store.py`: a local `BundleReader`
  Protocol (the read-only slice of the port; the worker does not import the API
  package) with `BundleStore` as its S3/MinIO backing.
- **Chart bundle-fetch init** — `charts/agentos/templates/agent-sandbox.yaml`
  uses the `mc` CLI, still a third dialect of the same S3 protocol (left as-is).

## Known leakage

The port now names the contract, but two physically separate S3 clients remain
(API/worker) plus the chart's `mc` init — fully unifying the client construction
(and the `mc` path) is left for when a second, non-S3 backend actually lands, at
which point the adapter is a drop-in `ObjectStore`/`BundleReader` rather than a
three-site sweep. Building that adapter ahead of a real non-S3 customer is still
out of scope (ADR-0007, ADR-0026).

## Cross-links

- **Epic(s):** #83 — vision epic for the blob-storage seam (extract a port only when a non-S3 backend lands).
- **Vision doc:** [architecture-vision.md](../../architecture-vision.md) — Job 4 (Blob storage), grade B+
- **ADR(s):** [ADR-0007](../../adr/0007-adopt-not-build-boundaries.md) — Adopt-not-build boundaries (MinIO adopted, AGPLv3, "offer BYO-S3")
