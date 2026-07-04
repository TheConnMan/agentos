# AgentOS

Open-source, self-hostable developer platform for Slack-based agents — the build workspace for the Curie AgentOS prototype. Connect Slack, author a Claude-Code-format plugin (skills + tools + MCP), deploy it as a versioned bot identity, and get traces + evals + budgets + git-flow for free.

This repo is where the prototype gets built. The architecture and every load-bearing decision below were validated on a real cluster on 2026-07-04 before any of it was committed — see `docs/` for the evidence.

## Status

**Pre-implementation.** The infrastructure foundation is proven end to end (Kubernetes Agent Sandbox runtime, claude-agent-sdk steering/interrupt/cache, Langfuse trace+eval backbone, security rails, warm-pool allocation, runner-in-sandbox with cache affinity). What remains is the build itself, ordered in `docs/mvp-build-plan.md`.

## Layout

```
docs/
  mvp-build-plan.md            The architecture + phased build order for the v0.1 MVP. Start here.
  prototype-derisking-review.md  The risk register: every assumption, ranked, with live evidence + the
                               remaining (non-prototype-settleable) risks.
  adr/                         Architecture Decision Records — the decisions, why, and the evidence.
  test-plans/                  PT-1..PT-4: the prototype test plans, each with its live run results.
  reference/                   The design corpus the build implements against:
                                 detailed-architecture.md   — the full component/ACI contract (the spec)
                                 on-prem-architecture.md     — build-vs-adopt research (license-verified)
                                 claude-design-prompt.md     — the AgentOS console UI spec (H1 lane)
                                 agent-architecture-decision-menu.md — the 10-axis decision menu
                                 product-direction.md        — the strategy/moat framing
prototypes/
  runner/                      The claude-agent-sdk runner proven inside a Sandbox (PT-E seed). Not prod.
  sdk-tests/                   Streaming steer/interrupt/cache probes (PT-2).
  observability/               OTLP emitter + Langfuse tool-tree reconstruction (PT-4).
```

## What's proven (all live, 2026-07-04)

| Claim | Result |
|---|---|
| Agent Sandbox routable control endpoint | `.status.serviceFQDN` + headless Service, reachable |
| Hibernation preserves the process? | No — cold restart. **Design stateless-first, rehydrate on resume.** |
| Warm-pool allocation | ~0.2s claim to a pre-warmed sandbox |
| claude-agent-sdk steering + interrupt | Real (mid-run redirect at tool boundary; interrupt aborts) |
| Prompt-cache reuse in a live session | Proven in-sandbox: call-2 cache_read == call-1 cache_creation (16045) |
| Langfuse tool-call-tree via public API | Reconstructs 3-level tree; model span → GENERATION |
| Single-node footprint | ~2.3 GB (not 16-20); ClickHouse needs AVX (pin ≤24.8 for old CPUs) |
| Security boundary | Egress + metadata blocked (with control), per-agent secret isolation, non-root RO-rootfs, gVisor |

## The one design constraint

Hibernation cold-restarts the runner (a suspended sandbox is deleted; resume is a new pod). So AgentOS is **stateless-first**: session state is externalized and a resumed thread rehydrates from history. Prompt-cache warmth is an optimization *within* a continuous claim, never assumed across a suspend. See `docs/adr/0003-*`.

## Next

Freeze `packages/aci-protocol` first (the versioned contract), then the walking skeleton, then git-flow/evals, then prod-hardening the concurrency kernel. Full ordering in `docs/mvp-build-plan.md` §5.
