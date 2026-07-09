# 12. A substrate-agnostic worker and a channel-agnostic runner (the thin-shim thesis)

Date: 2026-07-09
Status: Accepted

This is a retroactive record of a decision already built into main; the "what was
built" is tracked in the shipped lanes, this ADR records the "why" and what it
closes the door on.

## Context

The product has to run in three shapes from one codebase: a real Kubernetes
cluster (production and leave-behind), a laptop with Docker and no cluster
(developer middle mode), and an automated test with no Slack workspace at all.
If any of the core logic (the concurrency kernel, routing, budgets, kill switch,
resume path) learns which substrate it is on or which channel a message came
from, those three shapes fork into three code paths, and the leave-behind /
local-dev story dies quietly the first time a substrate-specific branch lands.

## Decision

The worker never learns its substrate and the runner never learns its channel.
Two seams enforce this, both real code, not aspiration:

- **Substrate seam.** The worker talks only to a `SandboxClient` Protocol
  ([`apps/worker/src/agentos_worker/sandbox/k8s.py:50`](../../apps/worker/src/agentos_worker/sandbox/k8s.py)).
  `KubernetesSandboxClient` (`k8s.py:101`) drives the agent-sandbox controller;
  `DockerSandboxClient`
  ([`sandbox/docker.py:93`](../../apps/worker/src/agentos_worker/sandbox/docker.py))
  runs the identical runner image as a local container. Everything above the
  protocol is byte-identical across modes.
- **Channel seam.** The worker reaches Slack only through a base URL
  (`SLACK_API_BASE_URL`,
  [`config.py:146`](../../apps/worker/src/agentos_worker/config.py)). The CLI
  stands up a local Slack Web API stub and mints the exact `QueuedSlackEvent`
  wire payload onto the same `agentos:runs` stream
  ([`cli/src/chat.rs:269`](../../cli/src/chat.rs)); the worker cannot tell the
  stub from Slack.

The single runner image, the ACI it speaks (ADR-0005), and the plugin bundle it
loads are identical in every mode. Only the thing that starts the container, and
the thing that delivered the message, differ, and neither is visible above the
seam.

## Alternatives considered

- **Build for Kubernetes only.** Rejected: it forecloses the laptop middle mode
  and forces a cluster into every small leave-behind. The Docker substrate is
  what lets the whole product loop (real model call included) run on one machine.
- **Managed cloud runtime (Bedrock AgentCore / Vertex / Foundry).** Rejected for
  the interactive tier for the same reason ADR-0002 rejected it: cloud-locked
  runtimes cannot leave behind on-prem, which is the product's differentiator.
- **Let the kernel special-case the substrate or the channel** (a `if k8s:` fork,
  or a direct Slack SDK call in the kernel). Rejected: this is the exact coupling
  the thesis exists to prevent. Its failure mode is silent because tests running
  in Docker mode still pass while production K8s behaviour diverges.

## Consequences

- Two substrate implementations and the CLI channel stub must stay behind their
  respective seams; a substrate- or channel-specific branch above the seam is a
  design error, not a shortcut.
- Local middle mode and CLI-driven E2E are first-class verification surfaces, not
  toys: the code they exercise is the code that runs in production.
- Whether the substrate seam is a genuine swap axis or core-with-fallback is still
  open (see [issue #86](https://github.com/curie-eng/agentos/issues/86)); this ADR
  fixes the invariant (the core is blind to the substrate), not the number of
  substrates we will ever ship.
- This is the concrete instance of the swappable-jobs discipline recorded in
  ADR-0016.
