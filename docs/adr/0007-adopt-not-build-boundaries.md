# 7. Adopt-not-build boundaries; build only five things

Date: 2026-07-04
Status: Accepted

**Superseded in part by [ADR-0013](0013-concurrency-and-delivery-model.md)**
(back-link added under [ADR-0045](0045-the-status-line-is-the-mutable-part-of-an-immutable-adr.md)):
the Decision below adopts "BullMQ + Valkey (queue)". 0013 rejected BullMQ and
records the as-built queue as Valkey Streams via `redis-py`; BullMQ appears
nowhere in the tree. Only that clause is dead — every other adopt/build/do-not-build
boundary below stands, including the Valkey half of it.

## Context

A small team cannot maintain a bespoke telemetry datastore, eval engine, queue, or agent runtime and still ship a product. The guiding principle from the on-prem research (`docs/reference/on-prem-architecture.md`, removed post-MVP; see git history) is to minimize what gets built and lean on license-clean open source for everything else.

## Decision

**Build only:** the web UI, the API server (agents/versions/deployments + git-flow), the Slack dispatcher, the worker+runner glue, the `agentos` CLI, and the umbrella Helm chart.

**Adopt** (all license-verified): Langfuse (traces + evals, MIT core — ADR 0004), Kubernetes Agent Sandbox (interactive runner substrate — ADR 0002), Slack Bolt (Socket Mode, MIT), claude-agent-sdk (harness — ADR 0005), BullMQ + Valkey (queue), vanilla Postgres (app state), OTel Collector, and the Claude Code plugin format verbatim (the distribution wedge — do not invent a format).

**Do not build:** a generic declarative-DAG / config-as-agent engine (commodity infra where the project has no edge; the product's value is the verification/eval layer, not the orchestration layer).

## Consequences

- Any urge to hand-roll storage, telemetry, the queue, or the interactive runner substrate is a design error — stop and escalate.
- Transitive dependencies (ClickHouse, MinIO, Valkey) arrive via Langfuse and are reused rather than run separately.
- License caveats to honor: MinIO is AGPLv3 (offer BYO-S3); Langfuse EE gates only admin/security features, the eval + trace core is MIT. Pin ClickHouse for the CPU-baseline gotcha (ADR 0004).
- The single on-prem / leave-behind path is the portable container; managed cloud runtimes are cloud-locked and out for the interactive tier.
