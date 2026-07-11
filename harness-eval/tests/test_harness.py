"""FakeDriver behavior and the run_harness delta pipeline.

The key test builds a driver where WITH_PRIMER runs pass and BASELINE runs fail
across several tasks (mixed tokens/errors), runs the harness end-to-end through
the real ``score_run``, and asserts the hand-computed rollups and deltas. Two
further tests prove the scorer seam is honored and that ``primer`` is forwarded
only on the WITH_PRIMER condition."""

from __future__ import annotations

from pathlib import Path

from harness_eval.driver import AgentRunSpec, FakeDriver
from harness_eval.harness import run_harness
from harness_eval.models import AgentRun, Condition, HarnessTask, TaskScore

_SKILL_GOOD = """---
name: my-skill
description: Does a thing.
allowed-tools:
  - Read
---
# My Skill
"""

_SKILL_WRONG_KEY = """---
name: my-skill
description: Does a thing.
tools:
  - Read
---
# My Skill
"""


def _skill_task(task_id: str) -> HarnessTask:
    return HarnessTask(
        id=task_id,
        title=f"Build {task_id}",
        category="build-skill",
        prompt="Author a skill.",
        landmine="allowed-tools not tools",
    )


def test_fake_driver_writes_workspace_and_returns_run(tmp_path: Path) -> None:
    task = _skill_task("t1")
    spec = AgentRunSpec(
        files={"skills/my-skill/SKILL.md": _SKILL_GOOD},
        transcript="did it",
        input_tokens=40,
        output_tokens=10,
        errors=1,
    )
    driver = FakeDriver({(task.id, Condition.WITH_PRIMER): spec}, base_dir=tmp_path)

    run = driver.run(task, Condition.WITH_PRIMER, primer="PRIMER")

    assert run.workspace.is_dir()
    assert (run.workspace / "skills/my-skill/SKILL.md").read_text() == _SKILL_GOOD
    assert run.transcript == "did it"
    assert run.input_tokens == 40
    assert run.output_tokens == 10
    assert run.total_tokens == 50
    assert run.errors == 1
    assert run.condition == Condition.WITH_PRIMER


def test_run_harness_delta_math(tmp_path: Path) -> None:
    tasks = [_skill_task("t1"), _skill_task("t2"), _skill_task("t3")]

    # BASELINE fails (wrong key), WITH_PRIMER passes. Mixed tokens/errors.
    fail = {"skills/my-skill/SKILL.md": _SKILL_WRONG_KEY}
    good = {"skills/my-skill/SKILL.md": _SKILL_GOOD}
    b = Condition.BASELINE
    p = Condition.WITH_PRIMER
    runs = {
        ("t1", b): AgentRunSpec(files=fail, input_tokens=100, output_tokens=50, errors=2),
        ("t2", b): AgentRunSpec(files=fail, input_tokens=200, output_tokens=100, errors=1),
        ("t3", b): AgentRunSpec(files=fail, input_tokens=120, output_tokens=80, errors=3),
        ("t1", p): AgentRunSpec(files=good, input_tokens=40, output_tokens=10, errors=0),
        ("t2", p): AgentRunSpec(files=good, input_tokens=60, output_tokens=20, errors=0),
        ("t3", p): AgentRunSpec(files=good, input_tokens=50, output_tokens=30, errors=1),
    }
    driver = FakeDriver(runs, base_dir=tmp_path)

    report = run_harness(tasks, driver, primer="PRIMER-TEXT")

    # Accuracy: all baseline fail, all primer pass.
    assert report.baseline.accuracy == 0.0
    assert report.with_primer.accuracy == 1.0
    assert report.accuracy_delta == 1.0

    # Tokens: baseline totals 150+300+200 = 650 (mean 650/3);
    # primer totals 50+80+80 = 210 (mean 70).
    assert report.baseline.mean_tokens == 650 / 3
    assert report.with_primer.mean_tokens == 70.0
    assert report.token_delta == 70.0 - 650 / 3

    # Errors: baseline 2+1+3 = 6 (rate 2.0); primer 0+0+1 = 1 (rate 1/3).
    assert report.baseline.error_rate == 2.0
    assert report.with_primer.error_rate == 1 / 3
    assert report.error_rate_delta == 1 / 3 - 2.0

    # Every run produced a TaskScore.
    assert len(report.scores) == 6
    assert all(isinstance(s, TaskScore) for s in report.scores)


def test_run_harness_honors_custom_scorer(tmp_path: Path) -> None:
    tasks = [_skill_task("t1"), _skill_task("t2")]
    # Workspaces would FAIL score_build_skill, but the custom scorer forces PASS,
    # proving run_harness routes scoring through the injected callable.
    fail = {"skills/my-skill/SKILL.md": _SKILL_WRONG_KEY}
    runs = {
        ("t1", Condition.BASELINE): AgentRunSpec(files=fail),
        ("t2", Condition.BASELINE): AgentRunSpec(files=fail),
        ("t1", Condition.WITH_PRIMER): AgentRunSpec(files=fail),
        ("t2", Condition.WITH_PRIMER): AgentRunSpec(files=fail),
    }
    driver = FakeDriver(runs, base_dir=tmp_path)

    def always_pass(task: HarnessTask, run: AgentRun) -> TaskScore:
        return TaskScore(
            task_id=task.id,
            condition=run.condition,
            success=True,
            detail="forced",
            total_tokens=run.total_tokens,
            errors=run.errors,
        )

    report = run_harness(tasks, driver, scorer=always_pass)
    assert len(report.scores) == 4
    assert all(s.success for s in report.scores)


def test_run_harness_forwards_primer_only_with_primer(tmp_path: Path) -> None:
    tasks = [_skill_task("t1")]
    good = {"skills/my-skill/SKILL.md": _SKILL_GOOD}
    runs = {
        ("t1", Condition.BASELINE): AgentRunSpec(files=good),
        ("t1", Condition.WITH_PRIMER): AgentRunSpec(files=good),
    }
    driver = FakeDriver(runs, base_dir=tmp_path)

    run_harness(tasks, driver, primer="THE-PRIMER")

    seen = {condition: passed_primer for (_task, condition, passed_primer) in driver.calls}
    assert seen[Condition.BASELINE] is None
    assert seen[Condition.WITH_PRIMER] == "THE-PRIMER"
