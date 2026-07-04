# Agent Architecture: The Decoupled Decision Menu

Companion to [agent-os-product-direction.md](agent-os-product-direction.md). That doc argues *what* to build and where the moat is. This doc factors the *how* into independent decisions, so we stop re-litigating the whole stack every time someone builds a demo.

The key realization (from the Socket-Mode-vs-AgentCore discussion): **the message-dispatch layer is completely separate from the agent runtime.** Once you see that one seam, the rest fall out. The architecture is a menu of ten axes, grouped into three layers — **infrastructure/composition** (1-7), **problem shape** (8-9), and **production readiness** (10). Most are independent, with a small number of hard couplings. Pick one item per axis; the rules below (and the interactive picker) tell you which combinations are legal and roughly how much you are choosing to build vs. buy.

## How the tool is organized: six sections (the call terminology)

The picker groups the ten axes into six labeled sections, smallest/most-fundamental at top, using the terminology from the agent-architecture calls (Brian's "inside the agent vs. outside the agent" frame):

| # | Section | Call terminology | Axes inside |
|---|---|---|---|
| 1 | **LLM Provider** | "the model" | Provider |
| 2 | **Memory** | "context / user storage" | Memory |
| 3 | **Agent Harness** | "agent harness" / agent development — the agentic loop, skills, workflows, MCPs (inside the agent) | Harness |
| 4 | **Agent Deployment** | "agent deployment / deployment infrastructure" (outside the agent — how it runs, scales, persists) | Ingress / chat acceptance · Process model (stateless vs persistent) · Deployment substrate |
| 5 | **Authentication** | "authentication / permissioning" — OAuth-delegated vs service account | Auth |
| 6 | **Tasks & Observability** | "telemetry / observability / leave-behind / evals" | Task predictability · Compliance · Observability |

Process model (pet vs cattle) sits under **Agent Deployment** because the calls frame stateless-vs-persistent as a *deployment context model*, not a harness choice. The Layer A/B/C grouping below is the conceptual rationale; the six sections above are the presentation order.

## Layer A — Infrastructure / composition (axes 1-7)

### Axis 1 — Ingress / chat acceptance (how a message arrives)
| Option | Mechanism | Public inbound endpoint? | Needs persistent process? |
|---|---|---|---|
| **Socket Mode** | Outbound WebSocket to Slack | **No** (dials out) | **Yes** — something must hold the socket |
| **Slack Events API** | Slack HTTP POSTs to you | **Yes** | No |
| **Web UI** | Browser to your HTTP app | **Yes** | No |
| **API / MCP** | Token-authed HTTP | Yes (or VPC-internal) | No |
| **CLI / batch** | stdin / job trigger | No | No |

Only constraint: Socket Mode requires *a* persistent component to hold the WebSocket (not necessarily the *agent* — a thin relay works, see Socket -> AgentCore).

### Axis 2 — Agent harness / framework (the agent loop)
| Option | What it is | Provider-locked? |
|---|---|---|
| **Hand-rolled** | Our own tool-use loop (Revenue Leak) | No |
| **Claude Agent SDK** | Anthropic's agent SDK (the harness behind Claude Code) | **Yes — Claude** |
| **Strands** | AWS's agent framework (AgentCore Template) | No |
| **OpenAI Agents SDK** | OpenAI's agent loop (Finance-AP) | Effectively OpenAI |
| **LangChain / LangGraph** | General orchestration; durable graphs | No |
| **Curie-native loop** | The platform's `Asst` hierarchy (verify-loop, fallback, step-bounding) | No (multi-provider) |
| **Cowork / Claude Code plugin** | Ship skills + slash-commands + MCP; the Cowork/Claude host runs the agentic loop — no harness code to maintain | Yes — host (Claude) |

### Axis 3 — Runtime process model (pet vs cattle)
| Option | Shape | Scales out? | Idle cost |
|---|---|---|---|
| **Pet** (persistent) | Long-lived process; may hold in-RAM state | Only if state is externalized | Always-on |
| **Cattle** (stateless/serverless) | Per-request/session, no state in process | Yes, automatically | Scale-to-zero |

### Axis 4 — Deployment substrate (the runtime tier)
About **runtime tier, not location.** The same portable container runs in our cloud, a customer cloud, or a customer datacenter, so we enumerate the **highest managed agent runtime each cloud offers**, plus the one portable tier that runs anywhere.

| Option | Tier | On-prem / leave-behind? | Process model |
|---|---|---|---|
| **Portable container (Docker / k8s)** | Bring-your-own substrate | **Yes** — our cloud, customer cloud, or on-prem hardware | Either |
| **AWS Bedrock AgentCore** | Managed agent runtime (AWS) | No | Cattle |
| **GCP Vertex AI Agent Engine** | Managed agent runtime (GCP) | No | Cattle |
| **Azure AI Foundry Agent Service** | Managed agent runtime (Azure) | No | Cattle |
| **Serverless functions (Vercel / Cloud Run)** | Generic serverless PaaS | No | Cattle |
| **Cowork plugin (no deploy)** | Installed into the user's own Cowork; nothing to deploy, host, or scale | **Runs in the user's Cowork** | Cattle (host-run) |

**The Cowork plugin is the truly minimal shape** (from the Jun 16 product call): *"a slack plugin people install, it uses their credentials to connect to each of the things, and we don't even deploy a thing — it magically does the work from their own cowork."* It collapses three axes onto the host: **deployment** (nothing to run), **authentication** (the installing user's own credentials, i.e. delegated/on-behalf-of), and **memory** (the host's context). Lowest build complexity and highest cross-client reuse on the menu — a plugin just installs. The trade is zero infra control and dependence on the customer running Cowork.

**The three managed runtimes are direct equivalents** — session-isolated serverless runtime + managed memory + managed identity:

| | AWS | GCP | Azure |
|---|---|---|---|
| **Runtime** | Bedrock AgentCore | Vertex AI Agent Engine | Azure AI Foundry Agent Service |
| **Managed memory** | AgentCore Memory | Agent Engine Sessions + Memory Bank | Managed threads |
| **Managed identity** | AgentCore Identity (3LO vault) | Google IAM / OAuth | Entra ID agent identity |

Portable container is the single on-prem / leave-behind path (on-prem just means the container on commodity hardware/VMs; it can even be in a cloud). Vanilla cloud VMs aren't a menu item — they're one place a portable container runs.

### Axis 5 — LLM provider
| Option | Notes |
|---|---|
| **Claude direct** | Anthropic API (`opus-4-8`); reached over egress from any substrate |
| **OpenAI** | Finance-AP (`gpt-5.5`) |
| **Gemini** | Vertex / direct |
| **Bedrock (native model)** | AWS's own models via Bedrock (e.g. Nova Pro — what the AgentCore Template runs); AWS-only, cloud-locked |
| **Multi-provider w/ fallback** | Curie-native loop already does this; you build the routing |

**Claude-via-Bedrock** (running Anthropic Claude *through* Bedrock) stays **off the menu** — distinct from **Bedrock native models** (Nova), which are on the menu and are what the AgentCore Template actually uses. A managed runtime's native model path is its own cloud's model service; running it against a different provider means egress to that provider's API.

### Axis 6 — Memory / state location
| Option | Where state lives | Compatible substrate |
|---|---|---|
| **In-process RAM** | The pod's memory | Pet, single-replica only |
| **External store** (Redis/Postgres) | A backing service you run | Pet (multi-replica) or cattle |
| **Managed** | The runtime's built-in memory (AgentCore Memory / Vertex Memory Bank / Foundry threads) | Any managed agent runtime |
| **None / stateless** | No conversation memory | Any |

### Axis 7 — Auth / identity model (whose authority the agent acts with)
| Option | The agent acts as... | Per-user data scoping? | Needs from ingress |
|---|---|---|---|
| **OAuth — user creds (3LO)** | The end user (per-user token vault) | **Yes** | Authenticated caller identity |
| **Agent-embedded RBAC** | Itself, enforcing the caller's role in-agent | Yes, if it knows the caller | Authenticated caller identity |
| **Service account** | A fixed machine identity, static scope | No | Nothing (anonymous OK) |

OAuth-user-creds gives least-privilege and pairs with per-user memory; all three managed runtimes provide the token vault, self-host it on a container. Service accounts are simplest but run with one broad static permission set (blast radius).

## Layer B — Problem shape (axes 8-9)

These describe the *task*, not the infrastructure. They don't usually create hard incompatibilities, but they should steer the Layer A picks — and the picker warns when they fight.

### Axis 8 — Task predictability (workflow vs agent)
| Option | Meaning | Implication |
|---|---|---|
| **Enumerable (workflow)** | You can list the subtasks in advance | A deterministic chain/router beats a full agent loop — simpler, cheaper, auditable |
| **Semi-structured** | Mostly known, some dynamic branches | A light agent or a router with a few agentic steps |
| **Open-ended (agent)** | The model decides the path at runtime | A full agent loop; the most to build and test |

Anthropic and Google both treat "can you enumerate the subtasks?" as the *primary* fork. The anti-pattern this guards against: reaching for a heavy agent framework when a prompt-chain would do (Anthropic: "start simple, add complexity only when demonstrably needed").

### Axis 9 — Compliance / determinism requirement
| Option | Meaning | Implication |
|---|---|---|
| **Standard** | No special audit / determinism needs | Whole menu is open |
| **Regulated / auditable** | Deterministic, logged, replayable execution; per-user attribution (finance/healthcare) | Favors durable-graph harnesses + per-user identity + audit-grade observability; a hard filter against open-ended autonomy |

## Layer C — Production readiness (axis 10)

### Axis 10 — Observability / eval requirement
| Option | Meaning | Build cost |
|---|---|---|
| **Basic logging** | stdout / structured logs | Low |
| **Tracing + metrics** | OTel traces, dashboards, error rates | Medium (self-hosted on a container; included on managed runtimes) |
| **Tracing + eval harness** | Plus offline eval / regression scoring + per-run version reproducibility | High — and it is the moat |

No competing chooser tool asks this, and it is exactly the gap the [thesis doc](agent-os-product-direction.md) names as the differentiator: the company that can run agents in production (evals, state, observability, drift) wins. Eval-grade observability needs run-to-result reproducibility (stamp a config hash per run).

## Build vs. buy: the complexity dimension

Each option carries a rough **build-and-maintenance complexity** weight. Hand-rolling everything on your own substrate with your own eval harness is the most complex; leaning on a managed runtime + vendor SDK + managed memory + basic logging is the least. The picker sums these into a score and band so two valid configs compare on effort, not just legality.

| Axis | Low-complexity | High-complexity |
|---|---|---|
| Harness | Cowork plugin (1) / vendor SDK / Strands (2) | hand-rolled (5) |
| Deployment | Cowork plugin — no deploy (1) / managed runtime / serverless (2) | portable container you run (4) |
| Memory | managed (1), none (0) | external store you run (4) |
| Auth | service account (1) | OAuth 3LO self-hosted (4; -2 on a managed runtime) |
| Process | cattle (2) | pet (3) |
| Provider | single (1) | multi-provider fallback (3) |
| Ingress | CLI (1) / HTTP (2) | Socket Mode relay (3) / web UI (3) |
| Task predictability | enumerable (1) | open-ended agent (3) |
| Compliance | standard (0) | regulated / auditable (3) |
| Observability | basic (0) | tracing + eval harness (4) |

The cheapest-to-build configs cluster on managed runtimes + standard/basic posture; the most portable/controllable/compliant configs cost the most. That tension *is* the decision.

## Portability / reuse: the generalization dimension

A second score, scored **inverse to complexity** (High = good). It answers the Andrusko/Palantir question from the [thesis doc](agent-os-product-direction.md): how much of this architecture **copies to the next client engagement** vs. is bespoke. The north-star is "assembled from primitives, not hand-written per client." Hand-rolled code and cloud-locked managed runtimes are bespoke (low reuse); a portable container + a prebuilt harness + a stateless agent + your own externalized state copy-paste across clients (high reuse). (Working name "Portability / reuse" — could also be Generalization or Repeatability.)

| Axis | High-reuse (copies across clients) | Low-reuse (bespoke per client) |
|---|---|---|
| Harness | Cowork plugin (5) / Curie-native (5) / Strands / SDKs / LangChain (4) | hand-rolled (1) |
| Deployment | Cowork plugin — installs anywhere (5) / portable container (5) | serverless (2); managed runtimes are cloud-locked (3) |
| Process | cattle / stateless (4) | pet / persistent (2) |
| Memory | external store (4) / none (4) | managed, cloud-locked (2); in-RAM (3) |
| Provider | multi-provider (5); direct API key (4) | Bedrock native = AWS-locked (3) |
| Ingress | socket / api / cli (5/4/4) | events / web, per-client endpoint config (3) |
| Auth | service account (4) | OAuth/RBAC, per-env vault (3) |
| Task | enumerable workflow (4) | open-ended (3) |
| Compliance | standard (4) | regulated, per-client config (3) |
| Observability | the reusable telemetry leave-behind: traced/eval (4) | basic (3) |

Sum the weights; band Low (<=32) / Medium (33-38) / High (>=39); bar maps 26..44. The dominant drivers are exactly the two from the calls: **hand-rolled harness (1) vs prebuilt (4-5)**, and **portable container (5) vs cloud-locked managed runtime (3) vs serverless (2)**. The three built demos all land Medium (each mixes one high-reuse and one bespoke choice) — the spread shows only when you push to the extremes. The two scores together make the real tradeoff legible: the lowest-build-effort configs (managed runtime) are often *not* the most reusable (cloud-locked), and the most reusable (portable container + prebuilt harness) costs more to build.

## What's coupled, what's independent

**Hard couplings (the picker enforces these as INVALID):**
- **C2 — Managed runtime (AgentCore / Vertex / Foundry) ⟹ cattle.**
- **C3 — Serverless functions ⟹ cattle.**
- **C4 — Cattle ⟹ not in-RAM memory.**
- **C6 — Managed memory ⟹ one of the three managed runtimes.**
- **C8 — Claude Agent SDK ⟹ provider = Claude.**

**Soft couplings (CAVEAT — valid but warned):** Socket Mode into a managed/serverless substrate needs a relay; pet + in-RAM = single replica; container + cloud LLM = egress (air-gap needs self-hosted); OpenAI SDK wants OpenAI; OAuth/RBAC need a caller identity (not CLI); OAuth off a managed runtime = self-host the vault; service account = blast-radius. **Layer B/C cross-warnings:** enumerable task = prefer a workflow over an agent loop; regulated + open-ended = prefer deterministic/durable execution; regulated + service-account = weak audit trail; eval-grade obs = needs run reproducibility; traced/eval on a container = self-host the telemetry pipeline.

## What we've already prototyped

| Demo | Ingress | Harness | Process | Deploy | Provider | Memory | Auth | Status |
|---|---|---|---|---|---|---|---|---|
| **Revenue Leak** (#790, Alex) | Socket Mode | Hand-rolled | Pet | Portable container | Claude direct | In-RAM | Service account | **Built (merged)** |
| **AgentCore Template** (#785, Brian) | Events API | Strands | Cattle | AWS Bedrock AgentCore | Bedrock (Nova Pro) | Managed | OAuth user creds | **PR #785 (unmerged)** |
| **Finance-AP** (Apoorv) | Web + Events | OpenAI SDK | Cattle | Serverless functions | OpenAI | None | Service account | **Built** |
| **Cowork plugin** (Entlify-style, Brian) | Slack (via Cowork) | Cowork plugin | Cattle | Cowork plugin (no deploy) | Claude (host) | Host-provided | OAuth — user's own creds | **Built (Entlify)** |
| **Deal Desk** (#819, Yichen) | Web UI (+ Slack Socket Mode) | Hand-rolled | Pet | Portable container | OpenAI (gpt-5.4) | In-RAM | Service account | **Built (merged)** |

Layer B/C values (axes 8-10): Revenue Leak = open-ended / standard / basic. AgentCore Template = open-ended / standard / tracing. Finance-AP = semi-structured / standard / basic. Cowork plugin = open-ended / standard / basic. Deal Desk = enumerable / standard / basic.

(Socket -> AgentCore — Socket Mode relay in front of a managed runtime — remains a discussed *pattern*, not a built prototype, so it is not listed here.)

The AgentCore Template runs **Amazon Nova Pro** (`us.amazon.nova-pro-v1:0`) on native Bedrock — verified against PR #785 (zero `claude`/`anthropic` references in the diff). It maps to the **Bedrock (native model)** provider option, which is distinct from the off-menu "Claude via Bedrock." Finance-AP is attributed to Apoorv per team knowledge; git commits on the folder are Brian + Alex (it ships via Vercel, not git CI).

**What this shows:** we've prototyped on AWS, on a portable container, and on serverless. We have **not** touched the GCP or Azure managed runtimes, have not built the customer-environment leave-behind, and — tellingly — **none of the demos has reached eval-grade observability (axis 10) or a regulated posture (axis 9).** They sit at standard/basic, which is exactly the production gap the thesis doc says is the moat.

## How to read the menu (decision order)

1. **Problem shape first (axes 8-9).** Can you enumerate the subtasks? Enumerable -> a workflow, not a full agent. Regulated/auditable -> deterministic durable execution + per-user identity, and rule out open-ended autonomy.
2. **Must compute/data stay in the customer's environment?** Yes -> Portable container (the only leave-behind tier; highest build effort). No -> a managed runtime in the customer's cloud (lowest build effort; managed memory + identity).
3. **Public-inbound-endpoint constraint (cloud OK)?** -> Socket Mode; on a managed runtime that means a relay (Socket -> AgentCore).
4. **Whose authority?** Per-user least-privilege -> OAuth (free vault on a managed runtime). No scoping -> service account.
5. **Observability bar (axis 10).** Basic for a demo; tracing for production; eval harness when the agent's quality must be measured over time (the moat investment).
6. **Provider + harness** are mostly free; Claude Agent SDK pins Claude, OpenAI SDK pins OpenAI.

## The compatibility rules, formally (spec for the picker)

Selections are `{ingress, harness, process, deploy, provider, memory, auth, task, compliance, obs}`. `managed-runtime = {agentcore, vertex-agent, foundry-agent}`. Verdicts: **valid**, **valid-with-caveat**, **invalid**.

```
INVALID if:
  deploy ∈ managed-runtime ∪ {serverless, cowork}  AND  process == pet      # C2/C3 (host-run = cattle)
  process == cattle                        AND  memory == ram               # C4
  memory == managed                        AND  deploy ∉ managed-runtime     # C6
  harness == claude-agent-sdk              AND  provider != claude-direct    # C8

CAVEAT (valid, warn) if:
  ingress == socket  AND  deploy ∈ managed-runtime ∪ {serverless}
      -> "Needs a separate persistent Socket Mode relay in front; the agent stays serverless."
  process == pet     AND  memory == ram
      -> "Single replica only; externalize state to scale out."
  deploy == container  AND  provider ∈ {claude-direct, openai, gemini, multi, bedrock-native}
      -> "On a portable container the model is reached over egress; an air-gapped site needs a self-hosted endpoint."
  provider == bedrock-native  AND  deploy ∈ {vertex-agent, foundry-agent}
      -> "Bedrock native models are AWS-only; on GCP/Azure use that cloud's native model (Gemini / Foundry models)."
  harness == openai-sdk  AND  provider != openai
      -> "OpenAI Agents SDK is built around OpenAI; other providers are awkward."
  auth ∈ {oauth-user, embedded-rbac}  AND  ingress == cli
      -> "No authenticated caller identity on this ingress to act on behalf of."
  auth == oauth-user  AND  deploy ∉ managed-runtime ∪ {cowork}
      -> "Self-host the OAuth broker / token vault (managed runtimes and the Cowork host provide one)."
  deploy == cowork  AND  harness != cowork-plugin
      -> "A Cowork plugin runs in the host's harness; pair it with the Cowork / Claude Code plugin harness."
  deploy == cowork  AND  auth != oauth-user
      -> "A Cowork plugin acts with the installing user's own credentials (delegated); service-account/RBAC don't apply."
  auth == service-account
      -> "Agent runs with one static permission set: no per-user scoping; mind blast radius."
  task == enumerable
      -> "Enumerable steps rarely need a full agent loop; a deterministic chain/router is simpler and cheaper."
  compliance == regulated  AND  task == open-ended
      -> "Regulated/auditable work favors deterministic, replayable execution; prefer enumerable/semi steps + a durable-graph harness over open-ended autonomy."
  compliance == regulated  AND  auth == service-account
      -> "Audit/attribution usually needs per-user identity; a shared service account weakens the audit trail."
  obs == eval
      -> "Eval-grade observability needs per-run version reproducibility (stamp a config hash per run) — the moat investment."
  obs ∈ {traced, eval}  AND  deploy == container
      -> "On a portable container you self-host the telemetry pipeline (OTel collector); managed runtimes include tracing."

Otherwise VALID. Build-and-maintenance complexity = sum of per-option weights (build-vs-buy table),
with -2 if auth == oauth-user AND deploy ∈ managed-runtime.
Band: Low (<=18) / Medium (19-26) / High (>=27); bar maps score 10..36 -> 0..100%.
If the vector matches a prototyped demo, name it.
```

## The interview tool (where this is heading)

The picker is the *expert* view: you already know the axes and you compose a config. The other half is an **interview tool** — a Typeform-style, one-question-per-screen flow that takes plain-language **requirements** and outputs a **recommended architecture** (a config across all axes) plus the two scores and a per-choice rationale, with a "open in the full picker" handoff. It's the inverse of the picker: requirements in, architecture out.

**Question flow (each maps to one or more axes):**
1. **Who's it for?** Internal helper for our own team · A client engagement.
2. **Where must it run?** Inside the user's own Cowork (a plugin they install) · We host it (Curie cloud) · In the client's cloud/VPC (leave-behind) · Fully on-prem / air-gapped.
3. **Data residency** — must the agent's data/compute stay in the client's environment? Yes · No.
4. **Which cloud is the client on?** AWS · GCP · Azure · None / not sure.
5. **How do people talk to it?** Slack · Web app · API/MCP · It lives in their Cowork.
6. **Network posture** — public endpoint OK, or must be outbound-only?
7. **Permissions** — act as each user (their access) or one shared service identity?
8. **Predictability** — can you list the steps in advance, or is it open-ended?
9. **Compliance** — any audit / regulatory requirement?
10. **Quality bar** — how will you measure it over time? Just logs · Dashboards/traces · Formal evals.
11. **Priority** — ship this one fast, or maximize reuse across future clients?

**Recommendation mapping (sketch):**
- Q2 = "inside their Cowork", **or** (Q1 = internal **and** Q11 = fast) -> recommend the **Cowork plugin shape** (harness = Cowork plugin, deploy = Cowork, auth = OAuth/their creds, memory = host, provider = Claude/host). Lowest complexity, highest reuse; it negates the deployment/auth/memory decisions.
- Q3 = Yes **or** Q2 = on-prem -> deploy = portable container; else Q4 cloud -> that cloud's managed runtime.
- Q6 = outbound-only -> ingress = Socket Mode (relay if on a managed runtime); else Q5 maps ingress.
- Q7 -> auth (per-user = OAuth/RBAC, shared = service account).
- Q8 enumerable -> task = enumerable + a simpler harness (Curie-native/SDK over hand-rolled); open-ended -> full agent loop.
- Q9 regulated -> compliance = regulated + nudge to durable/auditable execution + per-user identity + traced/eval obs.
- Q10 -> observability tier. Q11 max-reuse -> portable container + prebuilt harness (high portability); fast -> managed runtime / Cowork + an SDK.
- Provider defaults to Claude direct (or Bedrock-native on an AWS managed runtime).

The recommended config is always run through the same compatibility rules and the two scores, so the output is guaranteed valid and comes with its complexity + portability read. Build target: a self-contained `architecture-interview.html` reusing the picker's design system and rule/scoring logic.

## Why this framing matters

The demos differ on the Layer A axes, but per the [thesis doc](agent-os-product-direction.md) the moat is the **verification oracle + eval reproducibility** — Layer C, axis 10 — which none of the demos has reached. So the discipline is: **pick a house default per axis, allow client-justified exceptions, and stop spending the team's design budget re-deciding the menu per demo** — so the next round of effort goes into the eval/verify layer that actually differentiates us.
