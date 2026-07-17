"""EvalRunner tests: run eval_case turns through a fake runner and grade them."""

from __future__ import annotations

import asyncio

from agentos_worker.eval import (
    EvalCase,
    EvalRunner,
    EvalSuite,
    ExpectedStatus,
    Grader,
    GraderKind,
)

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


def test_classified_failure_final_fails_even_if_text_matches(make_eval_harness) -> None:
    async def go() -> None:
        async with make_eval_harness() as (base_url, fake, client):
            # The runner ends in a classified failure but its text contains the
            # expected string. The case must still FAIL (a failed turn can never
            # turn a PR check green).
            fake.responses = {"q": "the answer is 4"}
            fake.classified_failure_inputs = {"q"}
            suite = EvalSuite(
                name="s",
                cases=[EvalCase(id="c", input="q", grader=Grader(kind=CONTAINS, expected="4"))],
            )
            result = await EvalRunner(client).run(suite, base_url=base_url, version="v1")

            case = result.results[0]
            assert case.passed is False
            assert case.error is not None  # the failure reason is recorded
            assert result.summary() == "0/1 passed"

    asyncio.run(go())


def test_idle_awaiting_input_final_fails_even_if_text_matches(make_eval_harness) -> None:
    async def go() -> None:
        async with make_eval_harness() as (base_url, fake, client):
            # The turn ends idle-awaiting-input (an incomplete turn), yet its text
            # contains the expected string. Grading only a Done turn, the case must
            # still FAIL -- an incomplete turn can never turn a promotion gate green,
            # matching the CLI's Done-gate.
            fake.responses = {"q": "the answer is 4"}
            fake.idle_inputs = {"q"}
            suite = EvalSuite(
                name="s",
                cases=[EvalCase(id="c", input="q", grader=Grader(kind=CONTAINS, expected="4"))],
            )
            result = await EvalRunner(client).run(suite, base_url=base_url, version="v1")

            case = result.results[0]
            assert case.passed is False
            assert case.error is not None  # the incomplete-turn reason is recorded
            assert result.summary() == "0/1 passed"

    asyncio.run(go())


def test_fresh_conversation_by_default_reds_a_case_that_only_passes_from_history(
    make_eval_harness,
) -> None:
    """The #550 regression: a case that could only pass by inheriting a prior
    case's history goes RED under the fresh-conversation default.

    The recall case answers with the joined conversation so far. If it ran in the
    seed case's conversation, its answer would contain the seed's secret and the
    grader would pass -- the exact false green #550 removes. With the per-case
    reset, the recall case starts fresh, its answer omits the secret, and it
    fails. Ordering is no longer load-bearing."""

    async def go() -> None:
        async with make_eval_harness() as (base_url, fake, client):
            fake.responses = {"remember secret 42": "ok, noted"}
            fake.recall_inputs = {"what is the secret?"}
            suite = EvalSuite(
                name="leak",
                cases=[
                    EvalCase(
                        id="seed",
                        input="remember secret 42",
                        grader=Grader(kind=CONTAINS, expected="ok"),
                    ),
                    EvalCase(
                        id="recall",
                        input="what is the secret?",
                        grader=Grader(kind=CONTAINS, expected="42"),
                    ),
                ],
            )
            result = await EvalRunner(client).run(suite, base_url=base_url, version="v1")

            by_id = {r.case_id: r for r in result.results}
            assert by_id["seed"].passed is True
            # The recall case CANNOT see the seed's history, so it goes red.
            assert by_id["recall"].passed is False
            assert by_id["recall"].output == ""  # no prior turn leaked in
            assert result.summary() == "1/2 passed"
            # The runner was reset before every fresh-conversation case.
            assert fake.resets == 2

    asyncio.run(go())


def test_shared_history_opt_in_lets_a_case_inherit_prior_history(
    make_eval_harness,
) -> None:
    """The opt-in half of #550: a case marked ``shared_history`` skips the reset
    and deliberately inherits the prior case's conversation, so the same recall
    case that fails in isolation now passes."""

    async def go() -> None:
        async with make_eval_harness() as (base_url, fake, client):
            fake.responses = {"remember secret 42": "ok, noted"}
            fake.recall_inputs = {"what is the secret?"}
            suite = EvalSuite(
                name="chain",
                cases=[
                    EvalCase(
                        id="seed",
                        input="remember secret 42",
                        grader=Grader(kind=CONTAINS, expected="ok"),
                    ),
                    EvalCase(
                        id="recall",
                        input="what is the secret?",
                        grader=Grader(kind=CONTAINS, expected="42"),
                        shared_history=True,
                    ),
                ],
            )
            result = await EvalRunner(client).run(suite, base_url=base_url, version="v1")

            by_id = {r.case_id: r for r in result.results}
            assert by_id["seed"].passed is True
            # recall inherited the seed turn, so its answer carries the secret.
            assert by_id["recall"].passed is True
            assert "remember secret 42" in by_id["recall"].output
            assert result.summary() == "2/2 passed"
            # Reset ran before the seed case but NOT before the shared_history one.
            assert fake.resets == 1

    asyncio.run(go())


def test_a_failed_reset_fails_the_case_rather_than_leaking_history(
    make_eval_harness,
) -> None:
    """If isolation cannot be established (the reset endpoint errors), the case is
    failed with an error rather than run against a possibly-leaked conversation --
    a false green is exactly what #550 removes."""

    async def go() -> None:
        async with make_eval_harness() as (base_url, fake, client):
            fake.responses = {"q": "the answer is 4"}
            fake.fail_reset = True  # the runner cannot establish isolation
            suite = EvalSuite(
                name="s",
                cases=[EvalCase(id="c", input="q", grader=Grader(kind=CONTAINS, expected="4"))],
            )
            result = await EvalRunner(client).run(suite, base_url=base_url, version="v1")

            case = result.results[0]
            assert case.passed is False
            assert case.error is not None and "reset" in case.error
            assert result.summary() == "0/1 passed"

    asyncio.run(go())


def test_gate_blocked_turn_is_green_and_narrate_only_is_red(make_eval_harness) -> None:
    """The run-7 anti-correlation, encoded (issue #262): a case that asserts
    `awaiting-approval` with a match-anything grader scores PASS when the turn
    parked awaiting approval (the gate held) and FAIL when the turn merely
    completed (`done`, the agent narrated). Before this change the runner gated on
    `done`, so the gate-blocked turn was RED and the narrate-only turn was GREEN --
    scoring anti-correlated with safety."""

    async def go() -> None:
        async with make_eval_harness() as (base_url, fake, client):
            gated = EvalCase(
                id="gate",
                input="q",
                grader=Grader(kind=CONTAINS, expected=""),  # match anything
                expect_status=ExpectedStatus.AWAITING_APPROVAL,
            )

            # The gate held: the turn parked awaiting approval -> GREEN.
            fake.responses = {"q": "blocked the close"}
            fake.awaiting_approval_inputs = {"q"}
            held = await EvalRunner(client).run(
                EvalSuite(name="s", cases=[gated]), base_url=base_url, version="v1"
            )
            assert held.results[0].passed is True
            assert held.summary() == "1/1 passed"

            # The agent merely narrated and the turn completed (done) -> RED.
            fake.awaiting_approval_inputs = set()
            fake.responses = {"q": "I asked for approval"}
            narrated = await EvalRunner(client).run(
                EvalSuite(name="s", cases=[gated]), base_url=base_url, version="v1"
            )
            assert narrated.results[0].passed is False
            assert narrated.results[0].error is not None

            # Inverse guard: a default (done) case never passes on an
            # awaiting-approval final, so the default gate did not loosen.
            default_case = EvalCase(
                id="d", input="q", grader=Grader(kind=CONTAINS, expected="")
            )
            fake.awaiting_approval_inputs = {"q"}
            fake.responses = {"q": "blocked the close"}
            default_result = await EvalRunner(client).run(
                EvalSuite(name="s", cases=[default_case]), base_url=base_url, version="v1"
            )
            assert default_result.results[0].passed is False

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
