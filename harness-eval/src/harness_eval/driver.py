"""Agent drivers: the seam that actually runs a task under a condition.

``FakeDriver`` replays canned workspaces for deterministic tests and the CLI
smoke. ``ClaudeCodeDriver`` drives a real coding agent against a freshly seeded
bundle for dogfooding; it is never exercised by the unit tests and degrades to
zeros rather than raising on a malformed agent response.
"""

from __future__ import annotations

import itertools
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from .models import AgentRun, Condition, HarnessTask
from .primer import primer_prompt_prefix


class AgentDriver(Protocol):
    """Runs one task under one condition and returns the produced run."""

    def run(
        self, task: HarnessTask, condition: Condition, primer: str | None
    ) -> AgentRun: ...


class AgentRunSpec(BaseModel):
    """A canned agent run: the files it produced plus its token/error counts."""

    files: dict[str, str] = Field(default_factory=dict)
    transcript: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    errors: int = 0


class FakeDriver:
    """Replay ``AgentRunSpec`` fixtures keyed by ``(task_id, condition)``.

    Every call materializes a fresh unique workspace under ``base_dir`` and
    records ``(task, condition, primer)`` in ``calls`` so tests can assert the
    primer is forwarded only on the WITH_PRIMER condition.
    """

    def __init__(
        self,
        runs: dict[tuple[str, Condition], AgentRunSpec],
        base_dir: Path,
    ) -> None:
        self.runs = runs
        self.base_dir = base_dir
        self.calls: list[tuple[HarnessTask, Condition, str | None]] = []
        self._counter = itertools.count()

    def run(
        self, task: HarnessTask, condition: Condition, primer: str | None
    ) -> AgentRun:
        self.calls.append((task, condition, primer))
        spec = self.runs[(task.id, condition)]
        workspace = self.base_dir / f"{task.id}-{condition.value}-{next(self._counter)}"
        workspace.mkdir(parents=True, exist_ok=True)
        for rel, content in spec.files.items():
            target = workspace / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return AgentRun(
            task_id=task.id,
            condition=condition,
            workspace=workspace,
            transcript=spec.transcript,
            input_tokens=spec.input_tokens,
            output_tokens=spec.output_tokens,
            errors=spec.errors,
        )


class ClaudeCodeDriver:
    """Drive a real coding agent against a freshly seeded AgentOS bundle."""

    def __init__(
        self,
        agentos_bin: str = "agentos",
        claude_bin: str = "claude",
        model: str | None = None,
        base_dir: Path | None = None,
    ) -> None:
        self.agentos_bin = agentos_bin
        self.claude_bin = claude_bin
        self.model = model
        self.base_dir = base_dir
        self._counter = itertools.count()

    def run(
        self, task: HarnessTask, condition: Condition, primer: str | None
    ) -> AgentRun:
        workspace = self._seed_bundle(task)
        prompt = task.prompt
        if condition is Condition.WITH_PRIMER and primer:
            prompt = primer_prompt_prefix(primer) + "\n\n" + task.prompt
        # Isolate the run from host config so the baseline is a true control:
        # no host MCP servers (strict + empty config) and no user setting source
        # (excludes the host global CLAUDE.md, memory, and skills). Only the
        # injected prompt then differs between the two conditions.
        cmd = [
            self.claude_bin,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--permission-mode",
            "acceptEdits",
            "--allowedTools",
            "Edit,Write,Read,Bash",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers": {}}',
            "--setting-sources",
            "project,local",
        ]
        if self.model:
            cmd += ["--model", self.model]
        transcript, input_tokens, output_tokens, errors = self._invoke(cmd, workspace)
        return AgentRun(
            task_id=task.id,
            condition=condition,
            workspace=workspace,
            transcript=transcript,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            errors=errors,
        )

    def _seed_bundle(self, task: HarnessTask) -> Path:
        """Seed a bundle and neutralize it to a per-task failing start state.

        Runs ``agentos init`` under a unique parent, resolves the produced
        bundle, then strips whatever the default scaffold pre-satisfies so the
        scorer measures only the agent's change.
        """
        root = self.base_dir if self.base_dir is not None else Path(tempfile.mkdtemp())
        root.mkdir(parents=True, exist_ok=True)
        parent = root / f"{task.id}-{next(self._counter)}"
        parent.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                [self.agentos_bin, "init", task.id],
                cwd=parent,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            pass
        candidate = parent / task.id
        if candidate.is_dir():
            bundle = candidate
        else:
            bundle = parent
            bundle.mkdir(parents=True, exist_ok=True)
            (bundle / ".mcp.json").write_text('{"mcpServers": {}}\n')
        self._neutralize(bundle, task)
        return bundle

    @staticmethod
    def _neutralize(bundle: Path, task: HarnessTask) -> None:
        """Erase any default scaffold that pre-satisfies the task's scorer."""
        if task.category == "build-skill":
            skills = bundle / "skills"
            if skills.exists():
                shutil.rmtree(skills)
        elif task.category == "write-eval-gate":
            cases = bundle / "evals" / "cases.json"
            cases.parent.mkdir(parents=True, exist_ok=True)
            cases.write_text('{"cases": []}\n')
        elif task.category == "add-mcp-server":
            mcp = bundle / ".mcp.json"
            mcp.write_text('{"mcpServers": {}}\n')
        elif task.category == "fix-empty-api-key":
            (bundle / ".env").write_text('ANTHROPIC_API_KEY=""\n')

    def _invoke(self, cmd: list[str], workspace: Path) -> tuple[str, int, int, int]:
        """Run the agent and best-effort extract transcript/tokens/errors."""
        try:
            result = subprocess.run(
                cmd,
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=1800,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            # Missing binary, timeout, or other launch failure: a real error, not
            # a zero-error fail. subprocess.TimeoutExpired is a SubprocessError.
            return "", 0, 0, 1
        transcript, input_tokens, output_tokens, errors = self._parse_output(result.stdout)
        # A non-zero exit is an error even when stdout still parsed.
        if result.returncode != 0:
            errors = max(errors, 1)
        return transcript, input_tokens, output_tokens, errors

    @staticmethod
    def _parse_output(stdout: str) -> tuple[str, int, int, int]:
        try:
            payload: Any = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            return stdout, 0, 0, 0
        if not isinstance(payload, dict):
            return stdout, 0, 0, 0
        transcript = str(payload.get("result", ""))
        usage = payload.get("usage")
        input_tokens = 0
        output_tokens = 0
        if isinstance(usage, dict):
            # Cache tokens are additive with the base input count; omitting them
            # undercounts and can bias the two conditions differently.
            input_tokens = (
                _as_int(usage.get("input_tokens"))
                + _as_int(usage.get("cache_creation_input_tokens"))
                + _as_int(usage.get("cache_read_input_tokens"))
            )
            output_tokens = _as_int(usage.get("output_tokens"))
        errors = 1 if payload.get("is_error") else 0
        return transcript, input_tokens, output_tokens, errors


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
