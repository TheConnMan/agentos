"""CLI: run the primer before-after harness and print the delta report.

The ``fake`` driver replays a deterministic canned run set over the real task
catalog (no subprocess, no token spend) so the smoke always shows a positive
primer lift. The ``claude`` driver fetches the real primer and drives a live
coding agent for dogfooding.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from .driver import AgentRunSpec, ClaudeCodeDriver, FakeDriver
from .harness import run_harness
from .models import Condition, DeltaReport
from .primer import fetch_primer
from .report import render_markdown, render_summary
from .tasks import TASKS

# Canned fixtures per category: the workspace relpath plus its passing and
# failing contents. One table keyed by category, so the pass/fail workspaces
# cannot drift out of step.
_FIXTURES: dict[str, tuple[str, str, str]] = {
    "build-skill": (
        "skills/summarize/SKILL.md",
        (
            "---\nname: summarize\ndescription: Summarize text.\n"
            "allowed-tools:\n  - Read\n  - Bash\n---\n# Summarize\n\nBody.\n"
        ),
        (
            "---\nname: summarize\ndescription: Summarize text.\n"
            "tools:\n  - Read\n  - Bash\n---\n# Summarize\n\nBody.\n"
        ),
    ),
    "add-mcp-server": (
        ".mcp.json",
        '{"mcpServers": {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}}',
        '{"mcpServers": {"fetch": "mcp-server-fetch"}}',
    ),
    "write-eval-gate": (
        "evals/cases.json",
        (
            '{"cases": [{"id": "c1", "input": "2+2", '
            '"grader": {"kind": "contains", "expected": "4"}}]}'
        ),
        '{"cases": []}',
    ),
    "fix-empty-api-key": (
        ".env",
        "OTHER=1\nMODEL=claude\n",
        'ANTHROPIC_API_KEY=""\nOTHER=1\n',
    ),
}
# Baseline passes only the back half of the catalog; the primer passes all.
_BASELINE_PASSES = {"write-eval-gate", "fix-empty-api-key"}


def _canned_runs() -> dict[tuple[str, Condition], AgentRunSpec]:
    runs: dict[tuple[str, Condition], AgentRunSpec] = {}
    for task in TASKS:
        relpath, pass_content, fail_content = _FIXTURES[task.category]
        pass_files = {relpath: pass_content}
        fail_files = {relpath: fail_content}
        baseline_ok = task.category in _BASELINE_PASSES
        runs[(task.id, Condition.BASELINE)] = AgentRunSpec(
            files=pass_files if baseline_ok else fail_files,
            transcript="baseline run",
            input_tokens=180,
            output_tokens=90,
            errors=0 if baseline_ok else 2,
        )
        runs[(task.id, Condition.WITH_PRIMER)] = AgentRunSpec(
            files=pass_files,
            transcript="with-primer run",
            input_tokens=90,
            output_tokens=40,
            errors=0,
        )
    return runs


def _run_fake() -> DeltaReport:
    base_dir = Path(tempfile.mkdtemp(prefix="harness-eval-fake-"))
    driver = FakeDriver(_canned_runs(), base_dir=base_dir)
    return run_harness(TASKS, driver, primer="(fake primer)")


def _run_claude(model: str | None) -> DeltaReport:
    primer = fetch_primer()
    driver = ClaudeCodeDriver(model=model)
    return run_harness(TASKS, driver, primer=primer)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harness_eval")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the primer before-after harness")
    run.add_argument("--driver", choices=["fake", "claude"], default="fake")
    run.add_argument("--format", choices=["md", "json", "summary"], default="md")
    run.add_argument("--out", type=Path, default=None)
    run.add_argument("--model", default=None)
    args = parser.parse_args(argv)

    if args.driver == "claude":
        report = _run_claude(args.model)
    else:
        report = _run_fake()

    if args.format == "json":
        rendered = report.model_dump_json()
    elif args.format == "summary":
        rendered = render_summary(report)
    else:
        rendered = render_markdown(report, TASKS)

    if args.out is not None:
        args.out.write_text(rendered)
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
