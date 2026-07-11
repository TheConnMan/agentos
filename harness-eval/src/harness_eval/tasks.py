"""The task catalog: one realistic AgentOS task per scorer category.

Categories are bijective with ``scoring.SCORERS`` (every scorer has a task and
every task category has a scorer). Each prompt is a concrete instruction a
coding agent would act on inside a scaffolded bundle, and ``landmine`` names the
primer-taught gotcha the task is designed to expose.
"""

from __future__ import annotations

from .models import HarnessTask

TASKS: list[HarnessTask] = [
    HarnessTask(
        id="build-skill",
        title="Author a Claude skill",
        category="build-skill",
        prompt=(
            "Add a skill named summarize under skills/summarize/SKILL.md. It should "
            "have YAML frontmatter with a name, a description, and a restriction to "
            "the Read and Bash tools, then a short body describing what the skill "
            "does. Follow the Claude Code plugin skill format exactly."
        ),
        landmine="the tool restriction key is allowed-tools, not tools",
    ),
    HarnessTask(
        id="add-mcp-server",
        title="Register an MCP server",
        category="add-mcp-server",
        prompt=(
            "Register the fetch MCP server in this bundle's .mcp.json so the agent "
            "can fetch URLs. The server runs via uvx mcp-server-fetch. Wire it up "
            "under mcpServers using the Claude Code .mcp.json format."
        ),
        landmine="an mcpServers entry is an inline object with command/args, not a string pointer",
    ),
    HarnessTask(
        id="write-eval-gate",
        title="Write an eval gate",
        category="write-eval-gate",
        prompt=(
            "Add an eval suite at evals/cases.json with at least one case that "
            "checks the agent can add two numbers. Each case needs an id, an input "
            "prompt, and a grader that decides pass or fail from the answer."
        ),
        landmine="every eval case must name a grader; graders are deny-by-default",
    ),
    HarnessTask(
        id="fix-empty-api-key",
        title="Fix an empty API key",
        category="fix-empty-api-key",
        prompt=(
            "The .env in this bundle sets ANTHROPIC_API_KEY to an empty string, "
            "which silently breaks the CLI auth gate. Fix it so there is no "
            "residual empty ANTHROPIC_API_KEY assignment left behind."
        ),
        landmine="an empty ANTHROPIC_API_KEY assignment breaks auth; remove it or set a real value",
    ),
]
