# Primer before/after eval delta

## What this measures

This is the recorded result of the primer before/after harness: how a coding
agent's success on realistic AgentOS work changes when the AgentOS primer
(`agentos guide`) is placed in its context. The method:

- **The harness.** Every task runs under two conditions, baseline (no primer) and
  with primer, and a deterministic scorer grades the workspace the agent produced.
  See [`harness-eval/README.md`](../harness-eval/README.md) for the harness, the
  task set, and the scorers.
- **The task set.** Four realistic tasks, one per scorer category: author a Claude
  skill, register an MCP server, write an eval gate, and fix an empty API key.
  Each keys on one primer-taught landmine.
- **Deterministic scoring.** Each scorer inspects the produced files and returns
  pass or fail with no LLM judge, so a given workspace always scores the same way.
- **The two conditions.** The baseline sends the task prompt alone; the with-primer
  condition prepends the `agentos guide` output as an instruction preamble.
- **Isolation.** Each run isolates the agent from host configuration (strict MCP
  config, and settings sources limited to project and local, excluding the host
  user config), so the baseline is a true control with no ambient AgentOS
  knowledge; token counts include cache-read and cache-creation input tokens,
  which dominate, hence the large absolute values.

This work is the citation asset called for in
[ADR-0021](adr/0021-agentos-is-a-harness-for-coding-agents.md): publishing the
harness's own before/after eval delta as proof of the primer's value.

## Result

**Headline:** on this isolated pilot run, accuracy dropped from 75.0% baseline to
50.0% with the primer (n=1 per cell), and the primer raised mean token use per
task by about 124702 tokens; both conditions ran with zero errors.

Run details: a single trial per task-condition (n=1 per cell), 4 tasks x 2
conditions = 8 real `claude -p` runs, executed locally via the isolated
`ClaudeCodeDriver` on 2026-07-11 using the Claude Code default model. Primer text
came from `agentos guide`.

| Metric | Baseline | With primer | Delta |
| --- | --- | --- | --- |
| Accuracy | 75.0% (3/4) | 50.0% (2/4) | -25.0 points |
| Total tokens | 833093 | 1331901 | +498808 |
| Mean tokens/task | 208273.25 | 332975.25 | +124702.0 |
| Errors | 0 | 0 | +0.00 error rate |

Per task:

| Task | Category | Baseline | Baseline tokens | With primer | With-primer tokens |
| --- | --- | --- | --- | --- | --- |
| Author a Claude skill | `build-skill` | pass | 173407 | pass | 144801 |
| Register an MCP server | `add-mcp-server` | fail | 207658 | fail | 217448 |
| Write an eval gate | `write-eval-gate` | pass | 243001 | fail | 715774 |
| Fix an empty API key | `fix-empty-api-key` | pass | 209027 | pass | 253878 |

Notes on the tasks that did not pass:

- **`add-mcp-server` (fail in both arms).** Both the baseline and the with-primer
  run left `.mcp.json` with no `mcpServers` entry, so the primer did not flip this
  task under isolation.
- **`write-eval-gate` (pass to fail).** The baseline wrote a valid
  `{"cases": [{... "grader": ...}]}`. The with-primer run wrote a top-level JSON
  array with no `cases` key and no grader, which the scorer correctly failed,
  while burning far more tokens.

## Reading the result

This is an **n=1 pilot**. Its value is that it proves the harness runs end to end
against a real coding agent and produces the three metrics (accuracy, mean tokens,
error rate) plus the before/after delta. It is **not** a proof of the primer's
value.

On this isolated single sample the primer did not help: net accuracy went down 25
points. That drop is driven by the with-primer `write-eval-gate` run choosing a
non-conforming schema (a top-level array instead of a `cases` object), which
regressed a task the baseline had passed; `add-mcp-server` failed in both arms.
Token cost rose sharply with the primer in context.

A single trial per cell cannot separate the primer's effect from ordinary
run-to-run variance in a non-deterministic agent. A real delta needs many trials
per cell across multiple models, so the pass/fail and token figures average out; a
nominal single-sample regression like this one is exactly the noise that warning
guards against. This run is the reproducible starting point for that larger
measurement, not the published headline. Multi-sample, variance-aware evals are
tracked separately in issue #332.

## Reproduce this

```bash
uv run python -m harness_eval run --driver claude --format json --out harness-eval-result.json
```

This requires `agentos` and `claude` on `PATH` and a working model credential, and
it spends tokens (8 real agent runs). The numbers will differ run to run because
the agent is non-deterministic; treat any single run as one noisy sample.

## Publishable summary

> AgentOS ships a reproducible harness that measures how a coding agent's success
> on realistic AgentOS tasks changes with and without the AgentOS primer
> (`agentos guide`) in context, scoring each run deterministically against the
> specific landmine it targets, with every run isolated from host configuration so
> the baseline is a true no-knowledge control. In a first isolated n=1 pilot the
> primer did not help: net accuracy went down 25 points, driven by one task
> regressing on a non-conforming schema choice, and token cost rose sharply. That
> single trial is a proof the measurement works end to end, not a headline number:
> a meaningful before/after delta requires many trials per cell across models. The
> harness and its methodology are the deliverable; the headline numbers await a
> larger run.
>
> `Primer before-after: accuracy 75.0% -> 50.0% (-25.0 points), mean tokens +124702.0, error rate +0.00.`
