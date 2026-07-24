# 57. `cluster deploy` self-plumbs a port-forward so the generated key stays off the cleartext proxy

Date: 2026-07-20

Status: Accepted

Implements [#705](https://github.com/curie-eng/curie/issues/705).
Supersedes the deploy-transport decision of
[ADR-0024](0024-deploy-reaches-api-via-ui-proxy.md) for the auto path
(no `--api-url`). ADR-0024's UI `/api` NodePort proxy survives only as the
explicit-`--api-url` escape hatch, so 0024 is narrowed, not deleted: every other
property it established (the discovery it performs for `cluster status`, the
asymmetry rationale versus `cluster message`) is unchanged.

## Context

`curie cluster deploy` ships a bundle to the deployed release's platform API,
authenticated with an API key sent in the `X-API-Key` header. Two coupled
defaults made this unsafe once the key is auto-discovered.

Under ADR-0024, when `--api-url` was omitted, `cluster deploy` dialed the release
through the UI `/api` NodePort proxy: a cleartext HTTP path over a node port. And
until now the key defaulted to the `curie-dev-key` placeholder. As #705 wires
key auto-discovery (read the release's strong `<release>-secrets` `apiKey`), those
two defaults combine into a regression: the release's real, strong credential
would travel in cleartext over the NodePort proxy on every auto-path deploy.
Auto-discovering the strong key and sending it over a cleartext transport is the
exact pairing to avoid.

ADR-0024 abandoned an earlier self-plumbed port-forward (#352 / PR #357) for two
reasons: (i) it duplicated the port-forward machinery `cluster message` already
owned, and (ii) it added a child-process lifecycle to an otherwise pure-HTTP
verb. Both objections predate the shared, TCP-waited `start_port_forward` helper
that `cluster message` and `cluster eval` now reuse.

## Decision

For the auto path (no `--api-url`), `cluster deploy` self-plumbs a
`kubectl port-forward` to `svc/<release>-api`, a loopback tunnel, and posts to
`http://localhost:<local>`. It reuses the shared `start_port_forward` helper
(spawned `kill_on_drop` child, blocked until its local port accepts TCP) that
`cluster message` already owns. Because `svc/<release>-api` serves the platform
API at ROOT, the base URL carries no `/api` suffix; the `/api` in ADR-0024 was an
artifact of routing through the UI pod.

When `--api-key`/`CURIE_API_KEY` is omitted, the key is auto-discovered from
`<release>-secrets`; an explicit key wins. The resolved key flows only into the
`X-API-Key` header, never into any argv (the port-forward argv carries no key).

An explicit `--api-url` or `CURIE_API_URL` still direct-dials the given URL with
no tunnel, preserving ADR-0024's proxy path as the operator escape hatch.

ADR-0024's objections no longer hold: (i) is moot because `start_port_forward` is
now a shared, reused helper rather than duplicated machinery, and (ii) is accepted
as outweighed by the security requirement, at a cost of one `kill_on_drop` child
held until the deploy returns.

## Consequences

- The auto path never sends the release's strong key over the cleartext NodePort
  proxy: the key travels in the `X-API-Key` header over a loopback tunnel.
- The auto path needs no manual port-forward and no `--api-url`; the discovered
  key removes the `curie-dev-key` placeholder default.
- An explicit `--api-url` keeps direct dialing exactly as given, so ADR-0024's UI
  `/api` proxy remains available as the escape hatch.
- `cluster deploy` holds one `kill_on_drop` child for the tunnel, cleaned up on
  every exit path.
