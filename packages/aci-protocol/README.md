# aci-protocol

The frozen ACI (Agent Container Interface) contract: the
session protocol and NDJSON event types that every lane compiles against. The
Pydantic models here are the single source of truth; the committed JSON Schema,
the generated TypeScript, and the generated Rust are all derived from them.

## Stability

This is a frozen contract compiled against in three languages: the Pydantic
models are the source of truth, and the committed JSON Schema, generated
TypeScript, and generated Rust are derived from them. The runner, worker, CLI,
and UI all depend on it, so it never changes from a dependent lane; a needed
change stops the task and lands as its own reviewed change (see the
frozen-interface rule below). The wire contract is strict: the decoder accepts
only events whose `version` equals `PROTOCOL_VERSION` (currently `0.1.0`) and
raises `ProtocolVersionError` on anything else, with `version` typed as a
`const` literal so an off-version value is rejected at construction. The 0.x
line has no same-major looseness, so any protocol change is a `PROTOCOL_VERSION`
bump and a hard break signaled by `ProtocolVersionError`; a looser same-major
policy is planned for 1.0, and the schema-compat gate
(`tests/test_schema_compat.py`) fails on any drift.

## Contract surface (v0.1.0)

`PROTOCOL_VERSION = "0.1.0"` is embedded in the schema and in every outbound
event.

**Session setup** (`SessionConfig`, with `to_env()` / `from_env()`):

| Field | Env var | Notes |
|---|---|---|
| `plugin_dir` | `AGENTOS_PLUGIN_DIR` | mounted plugin bundle at pinned version |
| `session_id` | `AGENTOS_SESSION_ID` | thread-derived session id |
| `sandbox_id` | `AGENTOS_SANDBOX_ID` | claimed sandbox id |
| `budget` | `AGENTOS_BUDGET` | JSON `Budget` object |
| `memory_ref` | `AGENTOS_MEMORY_REF` | optional; S3 path / API URL |
| `credentials_ref` | `AGENTOS_CREDENTIALS` | optional; reference to injected secrets |
| `otel` | `OTEL_EXPORTER_OTLP_ENDPOINT` / `_HEADERS` / `_PROTOCOL` | optional |

`Budget` = `{max_output_tokens_per_run: int, task_budget_hint: int | None, max_usd_per_day: float}`.

**Inbound channel messages** (discriminated union on `kind`):

- `Event` = `{kind: "event", type: message|job|eval_case, text, user, ts}`
- `Interrupt` = `{kind: "interrupt", reason}`

**Outbound NDJSON response events** (discriminated union on `type`, each carries
`version`):

- `text_delta` = `{version, text}`
- `tool_note` = `{version, text, tool?}`
- `final` = `{version, text, status}` where `status` is `SessionStatus`
- `error` = `{version, message, classification?}`
- `side_effect_flag` = `{version, tool?, detail?}`

**Session status** (`SessionStatus`): `done`, `idle-awaiting-input`,
`classified-failure`.

**NDJSON helpers**: `to_ndjson_line`, `dump_ndjson`, `parse_ndjson_line`,
`parse_ndjson`, `iter_ndjson`, `parse_inbound`, `to_inbound_json`. The decoder
raises `ProtocolVersionError` for a missing or mismatched `version`.

**Conformance** (`run_conformance`, `reference_producer`): a reusable suite the runner
runs against the real runner. Pass a `Producer` (an inbound-message to NDJSON
function) to validate a real implementation's stream; the library round-trip and
version-rejection checks always run.

## Frozen-interface rule

This package is a **frozen interface**. Do not change it unilaterally from a
dependent lane. A needed change stops the current task and escalates to the
maintainers, who land the change as its own reviewed PR. Any change must:

1. bump `PROTOCOL_VERSION` in `src/aci_protocol/version.py`,
2. regenerate the committed artifacts with `scripts/check-contracts.sh`,
3. commit the regenerated schema and generated types together.

The compat gate enforces this: `tests/test_schema_compat.py` regenerates the
JSON Schema and Rust in-process and fails if the committed copies differ; CI
compiles the generated Rust (`cargo test`) and TypeScript (`tsc --noEmit`).

## Generated artifacts

- `schema/aci-protocol.schema.json` (committed) via `python -m aci_protocol.schema_export`
- `generated/rust/` (committed) via `python -m aci_protocol.rust_export`, a
  standalone serde crate proven by `cargo test`
- `generated/ts/aci-protocol.ts` (committed) via `json-schema-to-typescript`
  from the committed schema, proven by `tsc --noEmit`

## Decisions made under ambiguity (section 0 was underspecified here)

- **Inbound `kind` discriminator.** Section 0 lists `event` and `interrupt` as
  two separate channel operations without a shared tag. To make inbound frames
  self-describing on a single control channel (which the worker and runner need), inbound
  messages are a discriminated union on an added `kind` field. `Event.type`
  keeps its section-0 meaning (`message|job|eval_case`).
- **`credentials_ref` carries a reference, not secret material.** Section 0
  describes `AGENTOS_CREDENTIALS` as per-tool secrets via K8s Secret refs, so
  the typed contract holds the reference string, not the secrets.
- **`OtelConfig` captures a fixed subset.** `OTEL_EXPORTER_OTLP_*` is a wildcard
  in section 0; the typed view models the three standard fields the prototype
  used (`endpoint`, `headers`, `protocol`). Other OTEL vars pass through the
  environment untouched.
- **`final` carries `status`; `error` carries `classification`.** The output
  contract lists `done / idle-awaiting-input / classified failure` as ambient
  status. `final.status` carries the terminal session status; a classified
  failure surfaces as an `error` event plus a `final` with
  `status=classified-failure`. Wire tokens use hyphens (`idle-awaiting-input`,
  `classified-failure`) as spelled in section 0.
- **Version policy is exact match for the 0.x line.** The decoder accepts only
  events whose `version` equals `PROTOCOL_VERSION` and rejects anything else
  with `ProtocolVersionError`. The `version` field is typed as the literal
  `"0.1.0"`, so the models reject an off-version value at construction and the
  JSON Schema and TypeScript express it as a `const`, not just any string. This
  is the "reject unknown versions gracefully" requirement; a looser same-major
  policy can come with 1.0.
- **Whole-frame union types are exported.** The committed schema and generated
  TypeScript include `InboundMessage` (`Event | Interrupt`) and `OutboundEvent`
  (the five response events) as discriminated unions, so a consumer can type a
  full channel frame, not only the concrete variants. The generated Rust models
  these as internally-tagged enums that enforce the same strictness as Python:
  `deny_unknown_fields` on both structs and the tagged enums rejects extra keys,
  and the `version` field decodes through a guard that rejects any value other
  than `PROTOCOL_VERSION`.

## What consumers need to know

- **The runner:** implement the outbound stream with these exact event shapes
  and run `run_conformance(<your producer>)` in your suite; it must return
  `passed=True`. Read setup from the environment with `SessionConfig.from_env`.
- **The bundle pipeline:** this package does not validate bundles; see
  `plugin-format`.
- **The Rust CLI:** depend on the generated crate at
  `packages/aci-protocol/generated/rust` (or vendor its `lib.rs`); do not
  hand-write the types. The internally-tagged enums decode the NDJSON stream and
  inbound frames directly.
- **The UI (via TS):** import the interfaces and the `OutboundEvent` /
  `InboundMessage` union types from `generated/ts/aci-protocol.ts`.
