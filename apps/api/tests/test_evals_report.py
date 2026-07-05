"""POST /evals/report -> GitHub commit status (GitHub mocked via a fake reporter)."""

from typing import Any

from agentos_api.deps import get_github_reporter
from agentos_api.main import create_app
from fastapi.testclient import TestClient


class FakeReporter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int, int, str | None]] = []

    async def report_eval(
        self,
        repo_full_name: str,
        sha: str,
        passed_count: int,
        total: int,
        target_url: str | None = None,
    ) -> str:
        self.calls.append((repo_full_name, sha, passed_count, total, target_url))
        return "success" if passed_count == total and total else "failure"


def test_report_posts_status_and_returns_state(
    auth_headers: dict[str, str],
) -> None:
    reporter = FakeReporter()
    app = create_app()
    app.dependency_overrides[get_github_reporter] = lambda: reporter
    with TestClient(app) as client:
        resp = client.post(
            "/evals/report",
            json={
                "repo_full_name": "octo/demo",
                "sha": "abc123",
                "passed_count": 34,
                "total": 36,
                "target_url": "https://x/run",
            },
            headers=auth_headers,
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"state": "failure", "sha": "abc123"}
    assert reporter.calls == [("octo/demo", "abc123", 34, 36, "https://x/run")]


def test_report_success_state(auth_headers: dict[str, str]) -> None:
    reporter = FakeReporter()
    app = create_app()
    app.dependency_overrides[get_github_reporter] = lambda: reporter
    with TestClient(app) as client:
        resp = client.post(
            "/evals/report",
            json={
                "repo_full_name": "octo/demo",
                "sha": "abc",
                "passed_count": 36,
                "total": 36,
            },
            headers=auth_headers,
        )
    assert resp.json()["state"] == "success"


def test_report_requires_api_key(client: Any) -> None:
    resp = client.post(
        "/evals/report",
        json={"repo_full_name": "o/r", "sha": "s", "passed_count": 1, "total": 1},
    )
    assert resp.status_code == 401
