# 49. The boot env is a declared contract: BootEnv, producer ownership, and generated env keys

Date: 2026-07-17

Status: Accepted

Implements [#488](https://github.com/curie-eng/agentos/issues/488).
Builds on [ADR-0036](0036-aci-semver-and-reader-policy.md) (the semver and
reader policy this change is classed against) and preserves the seams
[#457](https://github.com/curie-eng/agentos/issues/457) and
[#429](https://github.com/curie-eng/agentos/issues/429) hardened.
Supersedes nothing.

## Context

The env the worker injects into a sandbox and the runner reads at boot is a
cross-lane contract that was never written down. `SessionConfig` froze six vars.
The other ~13 the runner behaviorally depends on lived as **paired string
literals**: a `*_ENV` constant in `apps/worker/src/agentos_worker/binding.py` and
a bare `env.get("AGENTOS_...")` in `runner/src/agentos_runner/config.py`. The
Rust CLI retyped a third copy, and the chart a fourth.

Rename either side of a pair and the sandbox boots, runs, and **silently drops
the feature**. Nothing fails: no type error, no missing key, no log line. That
failure mode is the whole reason for this change.

The surface had also accumulated stragglers that no producer ever set, so
"documented knob" and "wired knob" had quietly diverged.

## Decision

Declare the whole boot surface once, in `packages/aci-protocol`, and generate
every other lane's view of it from that declaration. The **wire does not move**;
only the declaration does.

### 1. A new `BootEnv` composing `SessionConfig`, not extending it

`SessionConfig` is the ACI v0.1 section-0 contract: the surface a third-party
ACI-conformant runner implements and what `run_conformance` checks. A runner
token, approval plumbing, a bundle ref, and a history port are **AgentOS platform
operations, not ACI**.

`BootEnv` is therefore the superset — `session: SessionConfig` as a *field*, plus
the platform-operational vars — and `SessionConfig` stays byte-identical.

**Rejected: extending `SessionConfig`.** Folding the platform vars in would tell
every future ACI implementer that a runner token and approval plumbing are part
of the interface, and would mutate a model the chart, CLI, and conformance suite
already read. The cost of the chosen split is two models where one would do, and
a nesting level on `BootEnv.session`. That nesting *is* the ACI-vs-platform
boundary, and making it visible is the point.

### 2. Env keys ride in the schema, so codegen emits them

The env key (`AGENTOS_RUNNER_TOKEN`) is not the field name (`runner_token`), so
the mapping must be machine-readable for codegen to emit constants. Each field
carries `json_schema_extra={"env": ..., "producer": [...]}`, and `rust_export`
grows a deterministic `pub mod env_keys` of `&str` constants that the CLI and the
chart render-assert pin against.

**Rejected: a separate `BOOT_ENV_KEYS` mapping.** It creates a second in-file
site that must stay in sync with the fields; the extra keeps the key on the field
itself. (Recorded fallback if `json-schema-to-typescript` had choked on the
unknown extras — it does not; verified, it passes them through.)

**Rejected: TypeScript constants.** `json-schema-to-typescript` emits types, not
const values, and no TS lane boots a sandbox.

### 3. Producer ownership: a SET, and authority is the invariant

Each key declares **every** producer that writes it, drawn from
`worker | kernel | substrate | operator`. Single ownership was a fiction the tree
contradicts. What matters is not the *arity* but the **authority class**, and
conflating the two is a live bug:

- **Substrate-authoritative** (`AGENTOS_SANDBOX_ID`, `AGENTOS_RUNNER_PORT`):
  `producers == {substrate}`; `worker` must never appear. Identity derives from
  the pod name via `fieldRef: metadata.name`, and because the chart sets
  `envVarsInjectionPolicy: Overrides` a worker write would **replace** the pod's
  real identity, breaking the "pod name IS the sandbox id" invariant that trace
  stamping (`otel.py`) and operator correlation depend on. The docker tier is
  only *incidentally* shielded by `_WORKER_OWNED_ENV`; k8s has no such shield.
- **Worker-authoritative with substrate fallback** (`ANTHROPIC_BASE_URL`,
  `AGENTOS_MODEL`, `AGENTOS_FAKE_MODEL`, `AGENTOS_CREDENTIALS`,
  `AGENTOS_PLUGIN_DIR`, `AGENTOS_SESSION_ID`, `AGENTOS_BUDGET`): genuinely two
  producers, and worker-wins under `Overrides` is **intended layering** — the
  chart branch is a baked template default keeping a warm, unclaimed pod
  bootable, the worker's value is per-claim routing and identity.

  The full set is what the runner container in `agent-sandbox.yaml` actually
  writes; the boot surface is the **runner's**, so `AGENTOS_BUNDLE_REF` staying
  worker-only is correct despite appearing in the chart — its two writes are in
  the `bundle-fetch` and `bundle-extract` **init containers**, which are not
  boot-env consumers.

So a multi-producer key is benign where the worker owns the truth, and a clobber
bug where the substrate does. A future reader who "simplifies" the set back to a
single value will either reintroduce the clobber or break the intended override.
A mandatory anti-clobber test pins the substrate-authoritative class against
worker emission.

### 4. Rendering is per-producer; there is no whole-model `to_env` on the wire

**Rejected: a single `to_env()` as "the" renderer.** The worker cannot construct
one — it does not know `sandbox_id` — and emitting the full union from the worker
*is* the clobber path above.

The worker gets the one real render surface, `render_worker(...)`, whose emitted
keys are a **subset** of the worker-producer keys with the difference exactly
`{AGENTOS_CONNECTOR_SECRET_KEYS}`. The kernel overlay and substrate set a few
individual keys and need only the constants, not a renderer. `from_env` remains
the single consumer parse of the full union, keeping `SessionConfig.from_env`'s
fail-loud `KeyError` on a missing `AGENTOS_SANDBOX_ID`: every real boot surface
supplies it, so a miss means a broken substrate and the runner should refuse to
boot.

**Rejected: loosening `sandbox_id` on the parse path.** It weakens existing
fail-loud behavior and drifts from frozen ACI semantics. The required-in-ACI /
not-worker-produced tension is reconciled by the producer tag instead.

`connector_secret_keys` is declared so the key is typed, exported, and parseable,
but `inject_connector_secrets` stays its **sole writer**. Making the model write
it would force the `#457` filter to run before construction, splitting the exact
seam that keeps `#429`'s connector-secret plaintext off the k8s claim CR.

### 5. Semver: PATCH, `0.2.1` -> `0.2.2`

Against the change-class table in `packages/CLAUDE.md`: every existing model is
untouched, and `BootEnv` is a **new top-level model no shipped consumer decodes**,
so no consumer can break on it — strictly more compatible than the table's
"new optional field -> patch" row, its most compatible entry. The rendered env is
byte-identical minus `AGENTOS_AGENT_ID`, which is in no schema and has no reader,
so its removal is not the table's "field removal" row. `from_env`'s required set
is unchanged: exactly `SessionConfig`'s existing six.

The `json_schema_extra` moves the wire fingerprint, so `wire.lock` fires. **That
is the gate working, not a signal to bump higher.** The gate was run and accepted
`0.2.2`.

### 6. Straggler verdicts

The rule applied: **per-agent-policy-shaped knobs that shadow an already-declared
surface get deleted; platform-wide bounds reachable through a documented operator
surface get declared.**

| Var | Verdict | Reason |
|---|---|---|
| `AGENTOS_AGENT_ID` | **DELETE** | Written by `binding.boot_env`, read by nothing. Not a field removal: it was never on the contract. |
| `AGENTOS_IDEMPOTENT_TOOLS` | **DELETE the env read** | Nothing sets it in any lane, so this removes zero shipped behavior. It *widens* the deny-by-default read-only allowlist the kernel's no-auto-retry-after-side-effects rule depends on, so wiring it through would hand any env-setting surface a widening knob over a safety flag. The in-process seam (`DEFAULT_IDEMPOTENT_TOOLS`, `SideEffectClassifier(idempotent_tools=...)`) is retained. A real per-agent surface belongs in its own change with a security review. |
| `AGENTOS_SYSTEM_PROMPT` | **DELETE the env read** | An *override* of the bundle's system prompt. The bundle is the declared surface; a second undeclared env path competing with it is exactly the shadow config this ADR exists to end. Nothing sets it. |
| `AGENTOS_MAX_TURNS` | **DECLARE** | A platform-wide runaway bound, not per-agent policy, and already reachable through the chart's `runner.extraEnv` and docker `-e` — a real documented operator surface. |
| `AGENTOS_HISTORY_MAX_TURNS` | **DECLARE** | Same class; it was read by a bare `os.environ` that bypassed the config loader entirely. Declaring pulls it into the single parse path. |
| `AGENTOS_HISTORY_MAX_BYTES` | **DECLARE** | Same as above. |

**Rejected: deleting the three knobs** (the cross-engine alternate's verdict, on
the premise that no declared producer sets them). The premise is false: the
chart's `runner.extraEnv` is a real operator surface that sets arbitrary runner
env today, and the overrides are shipped documentation. Deleting the reads would
silently break an operator using that surface.

The declared knobs are `int | None = None`, with defaults applied **consumer-side**
(max_turns 20, history 40 / 16_000). A non-None model default would render keys
no producer sends and move the wire. Parse tolerance is deliberately **not
unified**: `AGENTOS_MAX_TURNS` keeps its bare-`int` raise, and the history pair
keeps `_int_env`'s degrade-to-default on garbage *and* nonpositive values. A typo
in an operator's `extraEnv` must not become a boot crash where it used to
degrade. Unifying them would be a behavior change wearing a consistency costume.

### 7. Two parse deltas worth recording

**The `""` -> `None` coercion widened uniformly, and that is safe.** The plan
scoped it to `runner_token` (pre-change `config.py:105` was the only `or None`).
The shared `_str_or_none` helper now applies it to `model`, `history_ref`,
`bundle_ref`, `base_url`, `memory_token`, and `history_token` as well, where
pre-change `config.py:94,97` used a bare `env.get(...)` that preserved `""`.
Every widened field was chased for reachability and none is reachable: the chart
gates `AGENTOS_MODEL` behind `{{- else if $runner.model }}`
(`agent-sandbox.yaml:391`) so it is never emitted empty, `render_worker` omits
falsy values on all of them, and `AGENTOS_HISTORY_REF=""` was always already
equivalent to `None` via `resolve_history`'s `if not history_ref`
(`history.py:175`). One helper with one rule is the point of the freeze; a
per-field carve-out preserving an unreachable `""` would be shadow config of the
kind this ADR ends.

**Accepted delta: the tolerant int parse lost its diagnostic.** The deleted
`__main__._int_env` logged a warning on both the invalid and the nonpositive
path; `session._tolerant_int` degrades silently. The *value* behavior is
identical and pinned by test, but an operator who typos
`AGENTOS_HISTORY_MAX_TURNS=fourty` now gets the default with no signal, and
tolerance's whole justification is that a typo must not crash the boot. Accepted
here rather than fixed: `aci-protocol` is a frozen contract package with no
logging surface, and giving it one to serve a runner diagnostic would be scope
creep on this change. The fix belongs at the consumer, where the default is
applied. See Follow-ups.

## Consequences

- One declaration site. A boot key is typed once and every lane generates its
  view; a rename now breaks the build instead of silently dropping a feature.
- `.env.example` documents the boot surface from the model, drift-gated by
  `check-contracts.sh`.
- The `#457` reserved-boot-env pin is **retargeted**, not deleted: its dynamic
  discovery moves from the binding's (now removed) constants to `BootEnv`'s
  declared keys, keeping its non-vacuity floor. Dropping `AGENTOS_AGENT_ID` from
  the enumeration is policy-neutral — the `AGENTOS_` prefix catch-all still
  reserves the name, pinned by test.
- Cost: `BootEnv.session` nests, and the nine frozen `SessionConfig`/OTel keys
  need a companion producer map (they cannot be annotated in a frozen model),
  pinned by test against what `SessionConfig.to_env` actually writes.
- A rolling upgrade is a non-event: `render_worker`'s output is byte-identical to
  today's, and `AGENTOS_AGENT_ID` disappearing is safe precisely because nothing
  reads it.

## Follow-ups

- A per-agent idempotent-tools surface (DB column, API schema, CLI, migration,
  security review), if wanted at all.
- Restore the operator diagnostic on the tolerant history knobs (decision 7):
  log at the consumer where the default is applied (`__main__._load_history`)
  when the parsed value is `None` but the raw env was set and non-empty, keeping
  the logging out of the contract package.
- `plugin-format`'s `_AGENTOS_BOOT_KEYS` importing the key list from
  `aci-protocol` directly instead of pinning by test — a cross-frozen-package
  dependency direction that deserves its own ADR.
