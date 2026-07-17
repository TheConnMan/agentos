# Security Policy

AgentOS is a self-hostable platform for running Slack-based agents. This
document states the trust model plainly, explains how to report a
vulnerability, lists supported versions, and spells out what an operator is
responsible for.

## Trust model

**A bundle is code execution. The control is the sandbox, not the bundle
contents.**

The deployable unit in AgentOS is a "bundle": a Claude-Code-format plugin made
of skills, tools, and MCP servers. Uploading or deploying a bundle is
equivalent to running arbitrary code. AgentOS treats it that way by design:

- Bundle validation does not sandbox inputs at the config layer.
  [`packages/plugin-format/src/plugin_format/validate.py`](packages/plugin-format/src/plugin_format/validate.py)
  accepts any MCP server `command` (stdio) or `url` (remote) as-is. It only
  checks that one of the two is present, not what it points at. Model configs
  are `extra="allow"` by mandate.
- The runner executes the agent with `permission_mode="bypassPermissions"`
  ([`runner/src/agentos_runner/adapter.py`](runner/src/agentos_runner/adapter.py)).
  There is no in-agent permission gate.

Because a bundle is trusted to run arbitrary code, the security boundary is not
input validation. It is the Kubernetes sandbox rails shipped as defaults in the
Helm chart [`charts/agentos`](charts/agentos). See
[ADR-0006](docs/adr/0006-security-rails-as-chart-defaults.md) and
[`ARCHITECTURE.md`](ARCHITECTURE.md) for the full rationale.

### The sandbox rails

The rails are the actual control. Every agent runs inside them:

- **Default-deny egress** (`security.networkPolicy.enabled`). A NetworkPolicy on
  runner-sandbox pods is fail-closed: an empty `allowedEgress` means the sandbox
  can resolve DNS and ship traces, but reach nothing else. Arbitrary internet
  and the cloud metadata endpoint `169.254.169.254` are denied. Only
  explicitly-declared egress is permitted: DNS, the in-chart collector, MinIO,
  and inference when deployed, plus the operator's declared model API and MCP
  hosts via `allowedEgress`.
- **gVisor kernel isolation** via a RuntimeClass (`security.gvisor.mode`).
- **Non-root runner containers** with a read-only root filesystem.
- **Per-agent RBAC scoping** so one agent's secrets are isolated from another's. The chart ships a least-privilege baseline (a ServiceAccount with no bound Role and no mounted token); the control plane binds each agent's `resourceNames`-scoped Role when the agent is deployed.

**How to confirm the rails hold.** The chart ships a PT-3 security probe as a
`helm test`
([`charts/agentos/templates/security-probe.yaml`](charts/agentos/templates/security-probe.yaml))
that asserts all of the above. It includes a before/after control proving the
CNI actually enforces the egress policy, so a NetworkPolicy-unaware CNI cannot
silently false-pass. Run it against your cluster to verify enforcement.

**Caveat.** A NetworkPolicy is only enforced if the cluster's CNI supports it,
and gVisor only isolates if the RuntimeClass is installed. Both are operator
responsibilities (see below).

## Reporting a vulnerability

Please report security issues privately through **GitHub Private Vulnerability
Reporting**, which is enabled on this repository.

- Go to the repository's **Security** tab and click **Report a vulnerability**,
  or open a private advisory directly at
  <https://github.com/curie-eng/agentos/security/advisories/new>.
- **Do not** open a public issue or pull request for a vulnerability, and please
  do not disclose it publicly until a fix is available.

Include what you can:

- The affected version or commit.
- Steps to reproduce.
- The impact (what an attacker can do).

Maintainers will acknowledge your report and coordinate a fix and a
coordinated disclosure with you. There is no published security email; GitHub
Private Vulnerability Reporting is the channel.

## Supported versions

AgentOS is pre-1.0. Only the latest minor release line receives security
fixes; older lines are unsupported. See the
[Releases page](https://github.com/curie-eng/agentos/releases) for the current
version rather than a number pinned here (which only goes stale).

## Operator responsibilities

The rails are defaults, but they only protect a deployment the operator
configures and maintains. As an operator you are responsible for:

- **A CNI that enforces NetworkPolicy.** The default-deny egress rail is inert on
  a CNI that ignores NetworkPolicy. Verify with the security probe above.
- **Installing the gVisor RuntimeClass** referenced by `security.gvisor.mode`.
  Without it, the kernel-isolation rail does not isolate.
- **Keeping `allowedEgress` minimal.** Declare only the model API and MCP hosts
  your agents actually need. Every entry widens the sandbox.
- **Treating every bundle as trusted code.** Review what you deploy. A bundle
  can run anything the sandbox permits.
- **Protecting secrets and credentials** you supply to the platform (Slack
  tokens, model API keys), and relying on per-agent RBAC scoping to keep them
  isolated.
- **Running a supported version** and applying security updates promptly.
