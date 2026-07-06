# PT-2 — ACI round-trip: streaming steer + interrupt + spans + cache-read + plugin load

> **Historical document.** This is a prototype de-risking test plan from the pre-build phase, preserved as engineering history. It is not living documentation and is not maintained. See [`../prototype-derisking-review.md`](../prototype-derisking-review.md) for what these prototypes proved and the root [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md) for the system that got built.

Settles **R1 (control channel, second half)**, **R2 (prompt-cache warmth)**, **R7 (claude-agent-sdk hostable as a long-lived server)**, and **R8 (plugin bundle loads natively)**. This is the plan's spike **S2** (`detailed-architecture.md:232,357`), sharpened. Highest value-per-hour of the four because one round-trip exercises four risks together. Companion: `../analysis/agent-os-prototype-derisking-review.md` §3 R1/R2/R7/R8.

**Run status: CORE RUN locally via the OAuth token, 2026-07-04. Verdict: GO on the four hard uncertainties (auth, steer, interrupt, cache).** The full in-sandbox + Langfuse-span integration is deferred, but the claude-agent-sdk behaviors that PT-2 exists to de-risk are all confirmed. Live evidence below.

## Live results (local, claude-agent-sdk 0.2.110 + Claude CLI 2.1.201, driven by OAuth token, 2026-07-04)

A maintainer authorized using their Claude **OAuth token** (`claudeAiOauth.accessToken` from `~/.claude/.credentials.json`, scopes include `user:inference`, sub `max`) instead of a server API key. Set as `CLAUDE_CODE_OAUTH_TOKEN`; the SDK authenticated headless with no `sk-ant-...` key. **This clears PT-2's credential blocker for local/dev runs.**

- **Claim 1 (round-trip) + auth: PASS.** The SDK ran the agent loop headless on the OAuth token and returned `ResultMessage`s.
- **Claim 2 (steering mid-run): PASS at tool-loop boundaries.** Gave the agent the Bash tool and a 5-step sequenced task (`echo step-1 && sleep 2` … `step-5`); after `step-1`'s tool call landed, pushed a mid-run message ("CHANGE OF PLANS: … run exactly one command: `echo REDIRECTED`"). The agent's actual command sequence was **`[echo step-1 && sleep 2, echo REDIRECTED]`** — it abandoned steps 2-5 and obeyed the injected message. So steering is real (B2 confirmed), and it **lands at loop boundaries** (a pure-text single-turn task with no intra-turn boundary runs to completion first — observed in the first probe, where a counting task reached 30 before the queued steer applied). Design consequence for F1: "steering" needs either a tool/loop boundary or an explicit interrupt; a mid-turn text-only generation is not preempted by a queued message.
- **Claim 3 (interrupt): PASS.** `client.interrupt()` on an in-flight 1-to-100 essay task returned cleanly and the run **did not finish** (`reached_'hundred'=False`, `result_subtype=error_during_execution`). Interrupt aborts a live run.
- **Claim 5 (cache warmth within a session): PASS.** Two sequential turns in one session: turn 1 `cache_creation_input_tokens=26922, cache_read_input_tokens=13962`; **turn 2 `cache_read_input_tokens=40884`** (creation 15). Prompt caching works across consecutive turns within a single live session — confirming R2's *within-claim* half. (Combined with PT-1: cache warmth exists within a continuous claim but is destroyed by suspend/resume, since that cold-restarts the pod.)

**Deferred (not blocking the core):** Claim 4 (gen_ai spans → Langfuse from the runner) — the ingestion+tree path is independently proven in PT-4, so wiring the runner's exporter is mechanical. Claim 6 (plugin bundle load) — not yet exercised. Running the runner *inside* an Agent Sandbox on the scratch cluster — deferred; PT-1 already proved the sandbox routing, and the SDK behaviors above are substrate-independent. To finish PT-2 fully: build the ~150-line runner-server, mount a real plugin bundle, run it in a sandbox with `spec.service: true`, and point `OTEL_EXPORTER_OTLP_*` (HTTP/protobuf) at Langfuse.

### Note on OAuth vs API key for production
The OAuth token works for local/dev and for a maintainer's own runs. For a **customer leave-behind**, an OAuth user token is not the right production credential (it is a maintainer's personal Max-plan identity, rate-limited to them, and expires/refreshes); production runners still want a proper Anthropic API key / Bedrock / Vertex path per `detailed-architecture.md:207-214`. The OAuth path de-risks the *mechanism* cheaply; it is not the shipping auth model.

## Objective

Build the **smallest possible ACI runner** — a container that wraps the `claude-agent-sdk` streaming-input `query()` in an HTTP/WebSocket server — and prove, with captured evidence:

1. **Round-trip:** an initial event runs the agent loop and streams NDJSON response events back (`text_delta`/`tool_note`/`final`). (R7, R1)
2. **Steering:** a second event pushed **while the run is in flight** is incorporated at the next loop boundary — not queued behind a finished turn. (R1, B2)
3. **Interrupt:** an `interrupt` control stops the in-flight run promptly. (R1, B2)
4. **Spans:** the run emits OTel `gen_ai.*` spans that land in Langfuse as a nested generation+tool-call tree. (R3 cross-check; ties to PT-4)
5. **Cache warmth:** a second turn on the same warm session shows `cache_read_input_tokens > 0`. (R2)
6. **Plugin load:** a real Claude-Code-shaped plugin bundle (`plugin.json` + `skills/**/SKILL.md` + `.mcp.json` + `scripts/`) loads headless and a tool from its MCP server is callable. (R8, B8)

## Environment

- Runner container running **inside a claimed Agent Sandbox** on the scratch cluster (built on PT-1's outcome). If PT-1 returned NO-GO on routing, run the runner as a plain pod with a Service — PT-2's SDK/steer/cache findings are still valid; only the "over the sandbox route" nuance changes.
- **Anthropic credential:** the runner needs to reach the Anthropic Messages API. `ANTHROPIC_API_KEY` is currently **unset** in this shell and no key file is present beyond the interactive Claude CLI OAuth creds (`~/.claude/.credentials.json`), which are NOT a usable server API key. **This is a hard prerequisite** — see Blocker.
- Egress from the scratch cluster to `api.anthropic.com` must be allowed for this test (contrast PT-3, which proves egress is *blocked* by default — run PT-2 in a namespace WITHOUT the deny-all policy, or with `api.anthropic.com` explicitly allowed).
- Langfuse reachable (from PT-4, or a sidecar `langfuse` for this test). OTLP endpoint is `http://<langfuse>/api/public/otel/v1/traces` over **HTTP/protobuf** — NOT gRPC (gRPC OTLP is silently unsupported by Langfuse, verified 2026-07-04).

## Setup

```bash
# Minimal runner: Python, claude-agent-sdk, a tiny aiohttp/websockets server owning the query() generator.
# Pseudocode of the load-bearing shape (the streaming-input contract, verified against
# code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode):
#
#   inbox = asyncio.Queue()
#   async def message_gen():
#       while True:
#           ev = await inbox.get()          # initial event, then follow-ups pushed live
#           if ev is STOP: return
#           yield {"type":"user","message":{"role":"user","content": ev.text}}
#   client = ClaudeSDKClient(ClaudeAgentOptions(
#       max_turns=20,
#       # plugin bundle mounted at AGENTOS_PLUGIN_DIR; load skills + .mcp.json
#   ))
#   await client.query(message_gen())
#   async for msg in client.receive_response():   # stream deltas out as NDJSON over WS
#       ws.send(ndjson(msg))
#   # a POST /steer puts a message on inbox mid-run; POST /interrupt calls client.interrupt()
#
# OTel: set OTEL_EXPORTER_OTLP_ENDPOINT=http://langfuse/api/public/otel,
#       OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf,
#       OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental   # gen_ai semconv still Development
```

Build the image, load it into the scratch cluster (or `kind load docker-image`), and run it inside a sandbox/pod exposing the WS port via a Service.

## Commands / evidence capture

```bash
# 1. Round-trip: connect a websocat client, send an initial event, capture the NDJSON stream.
websocat ws://<runner-svc>:PORT/session/thread-1 <<<'{"type":"message","text":"List the files in the plugin scripts dir, then wait for my next instruction."}'
# EVIDENCE: text_delta/tool_note/final events stream back; a tool call fires.

# 2. Steering mid-run: start a long task, then push a second message BEFORE final.
# (script two sends: first "count slowly to 30 out loud, one number per line",
#  then ~2s later "actually stop counting and tell me a haiku instead")
# EVIDENCE: the transcript shows the agent changing course mid-run, not answering serially after 30.

# 3. Interrupt: start a long task, POST /interrupt.
curl -XPOST http://<runner-svc>:PORT/session/thread-1/interrupt -d '{"reason":"user stop"}'
# EVIDENCE: stream ends promptly with an interrupt/aborted marker; no further deltas.

# 4. Spans: after a run, query Langfuse for the trace.
curl -s -u $LF_PK:$LF_SK http://<langfuse>/api/public/traces/<traceId> | python3 -m json.tool
curl -s -u $LF_PK:$LF_SK "http://<langfuse>/api/public/observations?traceId=<traceId>" | python3 -m json.tool
# EVIDENCE: a generation observation + child tool observations with parentObservationId linkage.

# 5. Cache warmth: read the usage off two consecutive turns on the same session.
# EVIDENCE: turn 2's usage shows cache_read_input_tokens > 0 (the plan's :14 smoke criterion).
# Capture the raw usage block from the SDK result message or the Langfuse generation usage details.

# 6. Plugin load: bundle a real 2-skill + 1-MCP plugin (e.g. an MCP exposing one trivial tool),
# mount at AGENTOS_PLUGIN_DIR, and confirm the MCP tool is invoked in step 1's run.
# EVIDENCE: tool_note names the MCP tool; the MCP server process started (runner logs).
```

## Expected evidence

- **GO:** steering visibly redirects an in-flight run; interrupt stops it; a nested gen_ai trace appears in Langfuse; `cache_read_input_tokens > 0` on turn 2; the plugin's MCP tool is callable. This is the single strongest signal the interactive architecture is buildable as specified.
- **PARTIAL:** round-trip + spans + plugin all work but **cache_read is 0** (folds into R2 — investigate whether hibernation/TTL or prompt-prefix instability is the cause) OR steering queues instead of interrupting the turn (a claude-agent-sdk hosting nuance to design around).
- **NO-GO:** the SDK cannot be driven as a long-lived server that accepts mid-run input (would be surprising given the documented streaming mode, but it is exactly what R7 exists to check).

## Failure signals

- Traces missing entirely in Langfuse → almost certainly the **gRPC-vs-HTTP OTLP gotcha** (Langfuse rejects gRPC) or a missing root span (Langfuse "requires a root span or the trace is malformed"). Fix the exporter protocol first before concluding anything about span structure.
- `cache_read_input_tokens` always 0 across turns → prompt prefix is not stable (system prompt/tools reordered per turn) or the 5-min TTL lapsed between turns; distinguish these before blaming the architecture.
- Steering message only lands after the current turn completes → the SDK/host is queueing, not steering; note as an R1 design constraint (the "finish race" in F1 becomes even more central).

## Cleanup

```bash
kubectl delete namespace pt2   # runner pod, service
# revoke/rotate the scoped Anthropic key used for the test if it was minted for this
```

## Timebox

**1 to 1.5 days.** The runner-server is ~150 lines; most of the time is the OTel/Langfuse wiring and the steer/interrupt harness. If OTLP export fights back past 2 hours, park spans (defer to PT-4/S1 which isolates the ingest question) and keep the steer/interrupt/cache evidence, which is the higher-value half.

## Blocker (as of 2026-07-04)

Two prerequisites unmet:
1. **PT-1 outcome** (need a reachable sandbox, or accept a plain-pod substitute).
2. **A server-usable Anthropic API key** reachable from the scratch cluster. `ANTHROPIC_API_KEY` is unset here and the only local Anthropic creds are the interactive Claude CLI OAuth token (not a server key). **Exact next need:** a scoped `sk-ant-...` key (or a Bedrock/Vertex path) that can be injected as a K8s Secret into the runner namespace, plus egress from the scratch cluster to `api.anthropic.com`. Until then PT-2 cannot run; PT-1, PT-3, PT-4 do not need it and should go first.
