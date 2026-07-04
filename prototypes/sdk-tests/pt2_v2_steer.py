import anyio, time, json
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, AssistantMessage, TextBlock, ToolUseBlock, ResultMessage

# Steering at TOOL-LOOP boundaries: give the agent Bash and a multi-step task, then push a
# steer message mid-run. If steering is real, the agent changes course at the next loop boundary.

async def main():
    out = {"steer_at_toolboundary": None, "interrupt": None, "errors": []}

    opts = ClaudeAgentOptions(
        max_turns=15,
        allowed_tools=["Bash"],
        permission_mode="bypassPermissions",
        system_prompt="You are a test agent. Use the Bash tool. Obey the most recent instruction even if it changes your task.",
    )
    try:
        seen_tools = []; texts = []; pushed=False; t0=time.time()
        async with ClaudeSDKClient(opts) as client:
            await client.query("Use the Bash tool to run these one at a time, in order: `echo step-1 && sleep 2`, then `echo step-2 && sleep 2`, then `echo step-3 && sleep 2`, then `echo step-4 && sleep 2`, then `echo step-5`. Run each as a separate Bash call and tell me after each.")
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, ToolUseBlock):
                            seen_tools.append(str(b.input.get("command",""))[:40])
                            if not pushed and "step-1" in str(b.input.get("command","")):
                                # steer: after the first tool call lands, redirect
                                await client.query("CHANGE OF PLANS: stop the step sequence right now. Instead run exactly one Bash command: `echo REDIRECTED` and then stop.")
                                pushed=True
                        if isinstance(b, TextBlock):
                            texts.append(b.text)
                if isinstance(msg, ResultMessage):
                    break
        allcmds = " | ".join(seen_tools)
        out["steer_at_toolboundary"] = {
            "pushed_after_step1": pushed,
            "commands_run": seen_tools,
            "redirected_seen": any("REDIRECTED" in c for c in seen_tools),
            "ran_all_5_steps": sum(1 for c in seen_tools if "step-" in c) >= 5,
            "elapsed_s": round(time.time()-t0,1),
        }
    except Exception as e:
        out["errors"].append(f"steer: {type(e).__name__}: {e}")

    # Clean interrupt: capture partial output, interrupt, confirm it stopped before finishing.
    try:
        chunks=[]; result_seen={"v":None}
        async with ClaudeSDKClient(ClaudeAgentOptions(max_turns=4, allowed_tools=[])) as client:
            await client.query("Slowly write out the numbers 1 through 100 as words (one, two, three, ...). Do not stop early unless told.")
            async with anyio.create_task_group() as tg:
                async def do_int():
                    await anyio.sleep(3.5)
                    try:
                        await client.interrupt(); out["interrupt"]="interrupt() ok"
                    except Exception as e:
                        out["interrupt"]=f"interrupt() raised {type(e).__name__}: {e}"
                tg.start_soon(do_int)
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for b in msg.content:
                            if isinstance(b, TextBlock): chunks.append(b.text)
                    if isinstance(msg, ResultMessage):
                        result_seen["v"]=getattr(msg,"subtype",None) or "result"; break
        body=" ".join(chunks)
        out["interrupt"]=(out.get("interrupt") or "")+f" | reached_'hundred'={'hundred' in body.lower()} | result_subtype={result_seen['v']} | chars={len(body)}"
    except Exception as e:
        out["errors"].append(f"interrupt: {type(e).__name__}: {e}")

    print(json.dumps(out, indent=2, default=str))

anyio.run(main)
