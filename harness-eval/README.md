# harness-eval

A reproducible harness that measures a coding agent's task success on realistic
AgentOS work **with** versus **without** the AgentOS primer (`agentos guide`) in
context. Each task runs under two conditions (baseline and with primer), a
deterministic scorer grades the workspace the agent produced, and the harness
rolls the outcomes up into three metrics (accuracy, mean tokens, error rate) plus
the before/after delta between them. This is the measurement half of
[ADR-0021](../docs/adr/0021-agentos-is-a-harness-for-coding-agents.md): that ADR
commits to the primer as the harness's own citation asset, and publishing the
before/after eval delta is the consequence implemented under
[issue #326](https://github.com/curie-eng/agentos/issues/326). The primer text
being measured is emitted by `agentos guide` (issue #322).

## The task set

Four tasks, one per scorer category. Each is a concrete instruction a coding
agent would act on inside a scaffolded bundle, and each keys on one primer-taught
landmine that an unprimed agent tends to get wrong.

| Task | Category | What the agent is asked to do | Primer landmine |
| --- | --- | --- | --- |
| Author a Claude skill | `build-skill` | Add a `skills/summarize/SKILL.md` with YAML frontmatter (name, description, a tool restriction to Read and Bash) and a short body, following the Claude Code plugin skill format. | The tool restriction key is `allowed-tools`, not `tools`. |
| Register an MCP server | `add-mcp-server` | Register the `fetch` MCP server (runs via `uvx mcp-server-fetch`) under `mcpServers` in the bundle's `.mcp.json`, using the Claude Code `.mcp.json` format. | An `mcpServers` entry is an inline object with `command`/`args`, not a string pointer. |
| Write an eval gate | `write-eval-gate` | Add an eval suite at `evals/cases.json` with at least one case (id, input prompt, grader) that checks the agent can add two numbers. | Every eval case must name a grader; graders are deny-by-default. |
| Fix an empty API key | `fix-empty-api-key` | The bundle's `.env` sets `ANTHROPIC_API_KEY` to an empty string, silently breaking the CLI auth gate. Fix it so no residual empty assignment remains. | An empty `ANTHROPIC_API_KEY` assignment breaks auth; remove it or set a real value. |

## Scoring

Every scorer is **deterministic**: it inspects the files the agent produced in
the workspace and returns pass or fail with no LLM judge involved, so a given
workspace always scores the same way and results are reproducible. Each scorer
grades exactly the landmine its task keys on:

- **`build-skill`** passes when a `skills/**/SKILL.md` declares `allowed-tools` in
  its frontmatter. A file using `tools` instead fails.
- **`add-mcp-server`** passes when `.mcp.json` has at least one `mcpServers` entry
  that is an inline object carrying a `command`. String-pointer entries fail.
- **`write-eval-gate`** passes when `evals/cases.json` is a JSON object with a
  non-empty `cases` list and every case names a `grader`. A missing `cases` key,
  an empty list, or any case without a grader fails.
- **`fix-empty-api-key`** passes when no residual empty `ANTHROPIC_API_KEY=`
  assignment remains in `.env` (a removed assignment or a non-empty value both
  pass); an assignment left empty fails.

The scorer registry is **deny-by-default**: `score_run` looks the scorer up by
`task.category`, and a category with no registered scorer raises rather than
silently passing. Categories and scorers are bijective (every task has a scorer,
every scorer has a task).

Three metrics roll up across the task set, per condition:

- **accuracy** = passed / total tasks.
- **mean tokens** = total tokens / total tasks (input plus output).
- **error rate** = total agent errors / total tasks.

The reported **delta** is always `with_primer` minus `baseline`, so a positive
accuracy delta and a negative token or error delta read as the primer helping.

## How the real driver seeds a task

The `claude` driver builds each task's starting state from a real bundle rather
than a hand-written fixture. It runs `agentos init <task-id>` to scaffold a real
bundle, then **neutralizes** the parts the default scaffold already satisfies so
the scorer measures only the agent's own change:

- `build-skill`: remove the scaffold's default `skills/` directory.
- `write-eval-gate`: overwrite `evals/cases.json` with an empty `{"cases": []}`.
- `add-mcp-server`: ensure `.mcp.json` exists with an empty `mcpServers` map.
- `fix-empty-api-key`: seed `.env` with `ANTHROPIC_API_KEY=""`.

Each task therefore starts in a state the scorer fails, and the only way to a pass
is the agent making the intended change.

## Running it

### Deterministic smoke (no token spend)

The `fake` driver replays canned pass/fail workspaces over the real task catalog
with no subprocess and no token spend, so the smoke is fully deterministic and
always shows a positive primer lift. Use it in CI and for local sanity checks:

```bash
agentos dev harness-eval
# equivalently, from the package:
uv run python -m harness_eval run --driver fake
```

### Real before/after benchmark

The `claude` driver fetches the live primer via `agentos guide`, seeds each task
from a real bundle, and drives a real `claude -p` run per task-condition. It
requires both `agentos` and `claude` on `PATH` and a working model credential,
and it spends tokens:

```bash
uv run python -m harness_eval run --driver claude [--model <id>] [--format json] [--out <path>]
```

`--format json` emits the full `DeltaReport` (both rollups plus every per-run
score); the default `md` format renders the headline deltas and a per-task
pass/fail table. `--out` writes the rendered report to a file instead of stdout.

## Reproducibility and limits

The deterministic scorers and the fixed task set make the **structure** of the
result reproducible: the same workspace always scores the same way, and the metric
definitions never move. The **agent** under test is non-deterministic, so the
absolute numbers (pass/fail on any single task, token counts) vary from run to
run, and a single trial per cell is a noisy sample. A meaningful primer delta
needs many trials per cell across models. See
[`docs/harness-eval-delta.md`](../docs/harness-eval-delta.md) for the recorded
pilot result and an honest reading of what it does and does not show.
