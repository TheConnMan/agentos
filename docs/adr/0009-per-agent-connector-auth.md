# 9. Per-agent secrets and connector credentials

Date: 2026-07-09
Status: Proposed

## Context

An agent's usefulness comes largely from connectors: MCP servers that reach
GitHub, Jira, an internal API. A bundle declares those servers in `.mcp.json`,
and a stdio or remote MCP server almost always needs a secret (an API key, a
bearer token). The platform has no place for that secret today.

The only credential the platform resolves is the single model credential: it
flows from a chart Secret through `AGENTOS_CREDENTIALS` and is mapped by prefix
onto the SDK's auth env (`runner/src/agentos_runner/sdk_auth.py`), and an
explicit SDK credential already in the env wins. An `McpServer.env` field exists
in the plugin format but is static (literal strings baked into `.mcp.json`); the
Claude Code CLI expands `${VAR}` in those values, but the platform injects no
operator-provided variables into the runtime for such an expansion to resolve
against. Egress is default-deny (`charts/agentos/templates/security-networkpolicy.yaml`),
so a connector that reaches an external endpoint is dropped unless allowlisted.

Notably the frozen ACI already anticipates this: `SessionConfig.credentials_ref`
is documented as "per-tool secrets via K8s Secret refs ... the contract carries
the reference, not the secret material" (`packages/aci-protocol`). The seam
exists; only the resolution of it to more than the one model credential is
missing. This decision is about how a connector's secret is delivered into the
runtime, and it is independent of which harness runs the bundle.

## Decision

Adopt a **named, per-agent secret model, delivered as environment into the
sandbox, consumed by `.mcp.json` `${VAR}` expansion**, with a co-managed
per-agent egress allowlist.

- A bundle **declares which named secrets it needs** (versioned policy that ships
  and evaluates with the agent). The **values** are supplied as per-agent
  deployment configuration, never in the bundle.
- The platform resolves those bindings into a **per-agent Kubernetes Secret** and
  surfaces them into the runner/sandbox environment as named variables; the
  bundle's `.mcp.json` references them as `${VAR}`. Binding or rotating a secret
  requires a pod rollout, because `secretKeyRef` env resolves once at pod start.
- Reaching an external connector endpoint requires a **per-agent egress
  allowlist** entry; the default-deny posture is preserved.
- OAuth for remote MCP servers (dynamic client registration, token refresh) is
  **explicitly out of scope** here and deferred until a concrete need exists.

## Alternatives considered and rejected

1. **Bake secrets into the bundle's `.mcp.json`.** Rejected. Bundles are
   versioned, evaluable, reviewable artifacts that are often shared; a secret in
   the artifact leaks, appears in traces and git history, and cannot rotate
   without a redeploy. This breaks the "bundle is a reviewable artifact" premise.
2. **Extend the single-credential prefix map to carry N credentials in one blob.**
   Rejected. That path is Anthropic-model-auth-specific; overloading it with
   arbitrary third-party tool secrets conflates model authentication with tool
   authentication and does not scale to arbitrary named secrets.
3. **Put secret values (not just references) in the bundle manifest as deployment
   config.** Rejected. It conflates versioned policy (which secrets an agent
   needs) with workspace-specific values (the actual secret). The established
   behavior-packs split keeps policy in the bundle and bindings in deployment.
4. **A networked secret broker/vault the runner calls at tool-invocation time.**
   Rejected for now. It adds a runtime dependency and a new trust boundary reached
   from inside the untrusted sandbox path, and it is over-built versus a
   Kubernetes Secret plus env for the near term. Revisit only if short-lived or
   dynamically-minted secrets become a real requirement.
5. **Skip the platform-side mechanism and rely on the harness (OpenCode ships
   `{env:}` interpolation and MCP OAuth natively).** Not a rejection but a
   boundary: whatever the harness, the platform still must *deliver* the secret
   into the runtime environment. This decision is that delivery mechanism and is
   harness-independent; the harness only changes the consumption syntax. See the
   harness-strategy ADR.

## Consequences

- Connectors that need a secret become possible, which is most useful connectors.
- Egress must be co-managed with secrets: a credential without an allowlisted
  destination still fails closed, and an allowlist without a credential is inert.
- The rollout-restart requirement on bind is an operational cost to document
  (the same pattern the Slack connect verb already uses).
- OAuth-only remote MCP servers remain unsupported until the deferred OAuth work
  lands; if the harness strategy adopts OpenCode, its native OAuth may satisfy
  this instead of a bespoke broker.
