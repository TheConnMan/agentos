# INTERFACE: ACI producer (frozen protocol)

> Part of the AgentOS swappable-seam catalog — see the [seam index](../../interfaces.md).
> **Kind:** CLEAN &nbsp;·&nbsp; **Implementations today:** 1 + reference &nbsp;·&nbsp; **Swap-readiness grade:** A-

**Kind legend:** CLEAN = a real `Protocol`/typed port class · SOFT = swap via env/URL/prefix/wire, no code interface · NONE = not built yet.

## The black line

The frozen, cross-process ACI (Agent Container Interface) protocol — the strongest seam in the
system. It makes the whole **harness** swappable: anything inside the sandbox that speaks this
wire contract (session setup env + NDJSON event union + steer/interrupt endpoints) can replace the
default claude-agent-sdk runner without the worker, CLI, or UI changing. What stays opinionated
core is the protocol itself: the event shapes, the `AGENTOS_*` env contract, and the
compatibility-checked `version` gate. A second harness produces the same bytes; it does not get to
redefine them.

## Current contract

A second implementation is an **ACI server** — an HTTP process that accepts the three endpoints
(`docs/diagrams/aci.md:20`): `POST /v1/event` opens a turn, `POST /v1/steer` injects into the
live turn (409 if none running), `POST /v1/interrupt` hard-stops it. It streams the outbound
NDJSON discriminated union `OutboundEvent` (`packages/aci-protocol/src/aci_protocol/events.py:119`):
`TextDelta` (`text_delta`, :76), `ToolNote` (`tool_note`, :83), `Final` (`final` + `status`, :91),
`ErrorEvent` (`error` + `classification`, :99), `SideEffectFlag` (`side_effect_flag`, :107). Every
outbound event carries `version` equal to the producer's exact build `PROTOCOL_VERSION`; a consumer
accepts any `major.minor`-compatible version under 0.x (`major` after 1.0). Inbound frames are the
`InboundMessage` union `Event | Interrupt` on the `kind` tag
(`packages/aci-protocol/src/aci_protocol/events.py:64`, `:43`, `:55`). Setup is read from the
environment via `SessionConfig.from_env` (`packages/aci-protocol/src/aci_protocol/session.py:96`),
honoring the `AGENTOS_*` mapping in `to_env` (`:72`). Conformance is proven by
`run_conformance(<your producer>)`, which must return `passed=True`.

## Implementations today

One producer (the runner, `runner/src/agentos_runner/adapter.py`, a `ModelSession` wrapping
`ClaudeSDKClient`) plus the in-library `reference_producer` used by the conformance suite. The
contract is tri-language: Pydantic source of truth, committed JSON Schema in
`packages/aci-protocol/schema/`, and generated TS/Rust in `packages/aci-protocol/generated/`,
CI-guarded by `packages/aci-protocol/tests/test_schema_compat.py`.

## Known leakage

Plugin-format entanglement: the ACI server must interpret Claude Code plugin bundles mounted at
`AGENTOS_PLUGIN_DIR` (see the [bundle-format seam](../bundle-format/INTERFACE.md)), so a genuinely
foreign harness inherits that shape too — the A- is docked for exactly this and for the
SDK-shaped resume. Otherwise the line is clean and frozen: a producer constructs strictly (stray
keys are rejected at construction), while a consumer tolerates unknown fields and rejects only an
**incompatible** wire version, raising `ProtocolVersionError` naming both versions (see ADR-0036).

## Cross-links

- **Guide:** [implementing-an-aci-server.md](./implementing-an-aci-server.md) — stand up a conformant second ACI server, driven from the conformance suite (#256).
- **Epic(s):** [#25](https://github.com/curie-eng/agentos/issues/25) — write the "implement an ACI server" guide from the conformance suite so the port is documented, not just enforced
- **Epic(s):** [#47](https://github.com/curie-eng/agentos/issues/47) — telemetry as part of the ACI (OTEL carried in `SessionConfig`)
- **Vision doc:** [architecture-vision.md](../../architecture-vision.md) — Job 1 (Harness / runtime), grade A-
- **ADR(s):** [ADR-0005](../../adr/0005-claude-agent-sdk-adapter-and-frozen-aci.md) — claude-agent-sdk adapter behind a frozen ACI session contract
