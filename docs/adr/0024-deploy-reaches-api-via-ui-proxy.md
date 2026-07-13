# 24. `cluster deploy` reaches the platform API via the UI `/api` proxy

Date: 2026-07-13
Status: Accepted

Supersedes the abandoned self-plumbed port-forward approach for `cluster deploy`
(#352 / PR #357). Implements [#359](https://github.com/curie-eng/agentos/issues/359).

## Context

`agentos cluster deploy` ships a bundle to the deployed release's platform API
over plain HTTP (find-or-create agent, create version, upload bundle, create
deployment). Until now it defaulted `--api-url` to `http://localhost:8000` and
required the operator to first open a `kubectl port-forward svc/agentos-api`
themselves. That is a manual pre-step the CLI cannot verify, and the failure mode
(a connection refused against localhost) is opaque.

A prior attempt (#352 / PR #357) had `cluster deploy` self-plumb its own
port-forward. That was abandoned: it duplicates the port-forward machinery
`cluster message` already owns, and adds a child-process lifecycle to a verb that
otherwise just makes HTTP calls.

The release already exposes the UI on a NodePort by default (`cluster up` sets
`ui.service.type=NodePort` unless `--no-expose`), and the UI pod serves the
platform API under `/api`. So a routable path to the API already exists with no
tunnel: `http://<node-host>:<ui-nodeport>/api`.

## Decision

When `--api-url` (and `AGENTOS_API_URL`) is omitted, `cluster deploy`
auto-discovers the UI `/api` proxy URL and dials that. It reads the
`<release>-ui` service, requires it to be NodePort-exposed, resolves a routable
node host (kubeconfig `cluster.server` hostname, falling back to the first node's
InternalIP), and builds `http://<host>:<ui-nodeport>/api`. It **never** self-plumbs
a port-forward.

An explicit `--api-url` or `AGENTOS_API_URL` is still dialed exactly as given.
Every discovery failure (UI service unreadable, not NodePort-exposed, no assigned
nodePort, no resolvable host) is a usage error naming `--api-url` as the escape
hatch; the non-NodePort case also names `--no-expose` as the likely cause.

This is deliberately asymmetric with `cluster message`, which continues to
self-plumb a `kubectl port-forward`: `message` enqueues onto in-cluster Valkey,
which has no HTTP proxy, so a tunnel is the only path. `deploy` speaks only HTTP
to the API, and the UI's `/api` proxy is already an HTTP path, so no tunnel is
needed.

## Consequences

- `cluster deploy` works out of the box against a default (exposed) release with
  no manual port-forward and no `--api-url`.
- A `--no-expose` release has no NodePort proxy; `cluster deploy` there must pass
  `--api-url` explicitly. The usage error says so.
- On a managed/multi-node cluster the kubeconfig `cluster.server` host is
  typically a control-plane endpoint that does not expose Service NodePorts, so
  auto-discovery there yields an unreachable URL; the operator passes `--api-url`
  explicitly (the same escape hatch as `--no-expose`). This mirrors the discovery
  `cluster status` already performs and is a deliberate scope boundary of #359.
- `cluster deploy` gains `--namespace` / `--release` (default `agentos`) purely to
  locate the UI service for discovery; they do not change the shipped bytes.
- Discovery reuses the existing status-path host/service helpers (`resolve_node_host`,
  `parse_service`), so there is one definition of "the node host" and "read the UI
  service" across `cluster status` and `cluster deploy`.
- The port-forward-based deploy approach (#352 / PR #357) is not revived; the
  tunnel stays exclusive to `cluster message`.
