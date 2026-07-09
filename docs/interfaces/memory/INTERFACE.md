# INTERFACE: Memory

> Part of the AgentOS swappable-seam catalog — see the [seam index](../../interfaces.md).
> **Kind:** NONE &nbsp;·&nbsp; **Implementations today:** 0 loaders &nbsp;·&nbsp; **Swap-readiness grade:** not separately graded

**Kind legend:** CLEAN = a real `Protocol`/typed port class · SOFT = swap via env/URL/prefix/wire, no code interface · NONE = not built yet.

## The black line

There is no black line yet — memory is a **field only**. `SessionConfig` carries an optional
`memory_ref` (`packages/aci-protocol/src/aci_protocol/session.py:68`) mapped to the
`AGENTOS_MEMORY_REF` env var (`:85` in `to_env`, `:115` in `from_env`; documented as "optional;
S3 path / API URL" in the README contract table). The reference is plumbed end-to-end through the
session contract but **never dereferenced**: there are zero memory loaders in the codebase today.
The port (load / append / consolidate) is unbuilt on purpose — "the second implementation teaches
the interface," and there is no first loader to teach it yet.

## Current contract

The only committed surface is the optional string field:

```python
memory_ref: str | None = None   # session.py:68
```

and its env round-trip (`session.py:85`, `:115`). Nothing reads it. A future implementation must
define the port — sketched by epic #28 as `load(memory_ref) -> records`,
`append(record + provenance)`, `consolidate` — along with how `AGENTOS_MEMORY_REF` resolves (S3
path vs API URL) and the shape of a provenance record. None of that exists in code on this branch.

## Implementations today

Zero. `memory_ref` is carried by `SessionConfig` and forwarded into the sandbox environment; no
producer or consumer dereferences it.

## Known leakage

Not applicable — nothing is built to leak. The load-bearing constraint the future implementation
must honor: **memory lives OUTSIDE the sandbox.** Per ADR-0003 (stateless-first; session state is
externalized and a resumed thread rehydrates from history, never from a surviving in-RAM process),
the memory store must be an external, rehydratable resource resolved from `memory_ref`, not
in-pod state — an emptyDir-scratch or in-process cache would be lost on every suspend/resume.

## Cross-links

- **Epic(s):** [#28](https://github.com/curie-eng/agentos/issues/28) — define the memory port (`load` / `append` / `consolidate`), `AGENTOS_MEMORY_REF` resolution, and the provenance record shape
- **Vision doc:** [architecture-vision.md](../../architecture-vision.md) — memory is not one of the six swap-readiness Jobs; not separately graded
- **ADR(s):** [ADR-0003](../../adr/0003-stateless-first-rehydrate-on-resume.md) — stateless-first; rehydrate on resume; externalize session state
