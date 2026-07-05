# runner

Owning task: **D1**. The runner image and SDK adapter: productizes the PT-2/PT-E prototype into a streaming session server implementing the full ACI v0.1 contract (initial event, mid-run steer, interrupt, NDJSON stream, gen_ai spans), with `AGENTOS_BUDGET` enforcement, `side_effect_flag`, and plugin-bundle loading. Built on claude-agent-sdk (Python). Runs inside a claimed Agent Sandbox; the CLI (`agentos start`) also runs it locally in Docker. R0 ships only an empty importable skeleton so the workspace lint and test harness is green.
