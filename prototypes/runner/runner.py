"""Curie runner — prototype (PT-E seed).

Minimal claude-agent-sdk streaming server proven inside a Kubernetes Agent Sandbox
on 2026-07-04: it answered POST /run via the sandbox serviceFQDN, carried the prompt
cache across requests in one live session (call-2 cache_read == call-1 cache_creation),
and exported nested agent.run -> llm.generation gen_ai spans to Langfuse.

This is a de-risking prototype, NOT production code. The real ACI runner adds the full
session protocol (steer/interrupt/NDJSON streaming), CURIE_BUDGET enforcement, and the
side_effect_flag (see docs/reference/detailed-architecture.md section 0).

Env: CLAUDE_CODE_OAUTH_TOKEN (or a real API key in prod), OTEL_EXPORTER_OTLP_ENDPOINT
(e.g. http://<langfuse>/api/public/otel), LF_BASIC (base64 of pk:sk).
"""
import os
from aiohttp import web
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, AssistantMessage, TextBlock, ResultMessage
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.trace import SpanKind

LF = os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"]
auth = os.environ["LF_BASIC"]  # base64 pk:sk
exp = OTLPSpanExporter(endpoint=LF + "/v1/traces", headers={"Authorization": f"Basic {auth}"})
prov = TracerProvider(resource=Resource.create({"service.name": "curie-runner-sandbox"}))
prov.add_span_processor(SimpleSpanProcessor(exp))
trace.set_tracer_provider(prov)
tr = trace.get_tracer("curie")

client = None


async def get_client():
    """One long-lived SDK session per runner (per sandbox) — the source of cache affinity."""
    global client
    if client is None:
        client = ClaudeSDKClient(ClaudeAgentOptions(
            max_turns=4, allowed_tools=[],
            system_prompt="You are a terse sandbox test agent."))
        await client.__aenter__()
    return client


async def run(request):
    body = await request.json()
    text = body.get("text", "hello")
    c = await get_client()
    with tr.start_as_current_span("agent.run", kind=SpanKind.SERVER) as root:
        root.set_attribute("langfuse.trace.name", "curie-sandbox-run")
        out, usage = [], {}
        with tr.start_as_current_span("llm.generation") as gen:
            gen.set_attribute("model", "claude-agent-sandbox")
            await c.query(text)
            async for msg in c.receive_response():
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            out.append(b.text)
                if isinstance(msg, ResultMessage):
                    u = getattr(msg, "usage", None) or {}
                    usage = {k: (u.get(k) if isinstance(u, dict) else getattr(u, k, None))
                             for k in ("input_tokens", "cache_creation_input_tokens",
                                       "cache_read_input_tokens", "output_tokens")}
                    gen.set_attribute("gen_ai.usage.input_tokens", usage.get("input_tokens") or 0)
                    gen.set_attribute("gen_ai.usage.output_tokens", usage.get("output_tokens") or 0)
                    break
    return web.json_response({"text": "".join(out)[:200], "usage": usage,
                              "trace_id": format(root.get_span_context().trace_id, "032x")})


app = web.Application()
app.add_routes([web.post("/run", run),
                web.get("/healthz", lambda r: web.json_response({"ok": True}))])
web.run_app(app, host="0.0.0.0", port=8080)
