"""Task-catalog integrity: non-empty, unique ids, bijective category coverage
against the scorer registry, and every task fully populated."""

from __future__ import annotations

from harness_eval.scoring import SCORERS
from harness_eval.tasks import TASKS


def test_tasks_non_empty() -> None:
    assert len(TASKS) > 0


def test_task_ids_unique() -> None:
    ids = [task.id for task in TASKS]
    assert len(ids) == len(set(ids))


def test_task_categories_are_a_subset_of_scorers() -> None:
    categories = {task.category for task in TASKS}
    assert categories <= set(SCORERS)


def test_every_scorer_is_covered_by_a_task() -> None:
    categories = {task.category for task in TASKS}
    assert set(SCORERS) <= categories


def test_tasks_are_fully_populated() -> None:
    for task in TASKS:
        assert task.prompt.strip()
        assert task.title.strip()
        assert task.landmine.strip()
