"""PR-check reporter: exact GitHub commit-status payload (GitHub mocked)."""

import asyncio
import json
from typing import Any

import httpx
from agentos_api.github_checks import GitHubStatusReporter, eval_state


def test_eval_state_maps_rollup_to_status() -> None:
    assert eval_state(36, 36) == ("success", "36/36 passed")
    assert eval_state(34, 36) == ("failure", "34/36 passed")
    assert eval_state(0, 0) == ("failure", "0/0 passed")


def _run_report(passed: int, total: int) -> tuple[httpx.Request, str]:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(201, json={})

    async def go() -> tuple[httpx.Request, str]:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            reporter = GitHubStatusReporter(
                client,
                api_url="https://api.github.com",
                token="tok-123",
                context="agentos/evals",
            )
            state = await reporter.report_eval(
                "octo/demo", "abc123def", passed, total, target_url="https://x/run"
            )
        return captured[0], state

    return asyncio.run(go())


def test_report_eval_posts_the_exact_commit_status() -> None:
    request, state = _run_report(34, 36)
    assert state == "failure"
    assert str(request.url) == (
        "https://api.github.com/repos/octo/demo/statuses/abc123def"
    )
    assert request.method == "POST"
    body: dict[str, Any] = json.loads(request.content)
    assert body == {
        "state": "failure",
        "context": "agentos/evals",
        "description": "34/36 passed",
        "target_url": "https://x/run",
    }
    assert request.headers["Authorization"] == "Bearer tok-123"
    assert request.headers["Accept"] == "application/vnd.github+json"


def test_report_eval_success_state() -> None:
    _, state = _run_report(36, 36)
    assert state == "success"
