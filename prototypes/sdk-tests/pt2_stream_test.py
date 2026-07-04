import anyio, os, time, json
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, AssistantMessage, TextBlock, ResultMessage

# CLAUDE_CODE_OAUTH_TOKEN is set in the env from the creds file (workstream A).
# Force the SDK to NOT read the interactive login by relying only on the token env var.

async def main():
    results = {"auth": None, "steer": None, "interrupt": None, "cache": None, "errors": []}

    # --- Test 1+3: streaming steer, then a fresh interrupt, in one session ---
    opts = ClaudeAgentOptions(
        max_turns=8,
        allowed_tools=[],            # no tools; pure text so we isolate the steering mechanism
        system_prompt="You are a terse test assistant. Obey the latest instruction.",
    )
    steer_log = []
    try:
        async with ClaudeSDKClient(opts) as client:
            # Turn 1: a long, interruptible task
            await client.query("Count from 1 to 30, one number per line, and after each number write a short reflective sentence about it. Go slowly.")
            pushed = False
            t0 = time.time()
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            steer_log.append(b.text)
                            # As soon as we see the model has started counting, push a steer message.
                            if not pushed and ("1" in b.text or "one" in b.text.lower()):
                                await client.query("STOP counting immediately. Ignore the counting task. Reply with exactly the single word: BANANA")
                                pushed = True
                if isinstance(msg, ResultMessage):
                    # first turn's result
                    u = getattr(msg, "usage", None)
                    results["auth"] = "OK (got a ResultMessage)"
                    break
            joined = "\n".join(steer_log)
            # Steering worked if BANANA appears and we did NOT count all the way to 30
            results["steer"] = {
                "pushed_mid_run": pushed,
                "banana_seen": "BANANA" in joined.upper(),
                "reached_30": "30" in joined,
                "elapsed_s": round(time.time()-t0, 1),
                "sample_tail": joined[-400:],
            }
    except Exception as e:
        results["errors"].append(f"steer/auth: {type(e).__name__}: {e}")

    # --- Test cache: two sequential turns in a fresh session, read usage on turn 2 ---
    try:
        usages = []
        async with ClaudeSDKClient(ClaudeAgentOptions(max_turns=2, allowed_tools=[],
                system_prompt="You are a helpful assistant. "*40)) as client:  # big stable prefix to cache
            for turn, q in enumerate(["Say hello in one word.", "Now say goodbye in one word."]):
                await client.query(q)
                async for msg in client.receive_response():
                    if isinstance(msg, ResultMessage):
                        u = getattr(msg, "usage", None) or {}
                        usages.append(u)
                        break
        def g(u, k):
            if isinstance(u, dict): return u.get(k)
            return getattr(u, k, None)
        results["cache"] = {
            "turn1_usage": {k: g(usages[0], k) for k in ("input_tokens","cache_creation_input_tokens","cache_read_input_tokens","output_tokens")} if usages else None,
            "turn2_usage": {k: g(usages[1], k) for k in ("input_tokens","cache_creation_input_tokens","cache_read_input_tokens","output_tokens")} if len(usages)>1 else None,
        }
    except Exception as e:
        results["errors"].append(f"cache: {type(e).__name__}: {e}")

    # --- Test interrupt (native control) ---
    try:
        async with ClaudeSDKClient(ClaudeAgentOptions(max_turns=6, allowed_tools=[])) as client:
            await client.query("Write a 500-word essay about the history of the number zero. Take your time.")
            got_any = False
            async def reader():
                nonlocal got_any
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        got_any = True
                    if isinstance(msg, ResultMessage):
                        return "completed"
                return "stream_ended"
            async with anyio.create_task_group() as tg:
                async def do_interrupt():
                    await anyio.sleep(3.0)
                    try:
                        await client.interrupt()
                        results["interrupt"] = "interrupt() call succeeded"
                    except Exception as e:
                        results["interrupt"] = f"interrupt() raised: {type(e).__name__}: {e}"
                tg.start_soon(do_interrupt)
                outcome = await reader()
            results["interrupt"] = (results.get("interrupt") or "") + f" | stream outcome: {outcome}, got_output={got_any}"
    except Exception as e:
        results["errors"].append(f"interrupt: {type(e).__name__}: {e}")

    print(json.dumps(results, indent=2, default=str))

anyio.run(main)
