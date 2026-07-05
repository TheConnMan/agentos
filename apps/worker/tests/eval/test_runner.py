"""EvalRunner tests: run eval_case turns through a fake runner and grade them."""

from __future__ import annotations

import asyncio

from agentos_worker.eval import EvalCase, EvalRunner, EvalSuite, Grader, GraderKind

CONTAINS = GraderKind.CONTAINS


def test_runs_and_grades_a_mixed_suite(make_eval_harness) -> None:
    async def go() -> None:
        async with make_eval_harness() as (base_url, fake, client):
            fake.responses = {"2+2": "the answer is 4", "capital of France": "Berlin"}
            suite = EvalSuite(
                name="basics",
                cases=[
                    EvalCase(id="math", input="2+2", grader=Grader(kind=CONTAINS, expected="4")),
                    EvalCase(
                        id="geo",
                        input="capital of France",
                        grader=Grader(kind=CONTAINS, expected="Paris"),
                    ),
                ],
            )
            result = await EvalRunner(client).run(suite, base_url=base_url, version="v-abc")

            assert result.version == "v-abc"
            assert result.total == 2
            assert result.summary() == "1/2 passed"
            by_id = {r.case_id: r for r in result.results}
            assert by_id["math"].passed is True
            assert by_id["geo"].passed is False and by_id["geo"].output == "Berlin"
            # The case was delivered as an eval_case event, not a message.
            assert all(frame["type"] == "eval_case" for frame in fake.seen)

    asyncio.run(go())


def test_a_case_that_errors_is_failed_not_fatal(make_eval_harness) -> None:
    async def go() -> None:
        async with make_eval_harness() as (base_url, fake, client):
            fake.fail_inputs = {"explode"}
            fake.responses = {"fine": "yes"}
            suite = EvalSuite(
                name="s",
                cases=[
                    EvalCase(
                        id="bad", input="explode", grader=Grader(kind=CONTAINS, expected="x")
                    ),
                    EvalCase(id="good", input="fine", grader=Grader(kind=CONTAINS, expected="yes")),
                ],
            )
            result = await EvalRunner(client).run(suite, base_url=base_url, version="v1")

            by_id = {r.case_id: r for r in result.results}
            assert by_id["bad"].passed is False and by_id["bad"].error is not None
            assert by_id["good"].passed is True  # one bad case did not abort the suite
            assert result.summary() == "1/2 passed"

    asyncio.run(go())


def test_all_passed_suite(make_eval_harness) -> None:
    async def go() -> None:
        async with make_eval_harness() as (base_url, fake, client):
            fake.responses = {"q1": "a1", "q2": "a2"}
            suite = EvalSuite(
                name="green",
                cases=[
                    EvalCase(id="1", input="q1", grader=Grader(kind=CONTAINS, expected="a1")),
                    EvalCase(id="2", input="q2", grader=Grader(kind=CONTAINS, expected="a2")),
                ],
            )
            result = await EvalRunner(client).run(suite, base_url=base_url, version="v1")
            assert result.all_passed() is True
            assert result.summary() == "2/2 passed"

    asyncio.run(go())
