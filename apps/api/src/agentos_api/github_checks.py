"""PR-check reporter (K1): turn an eval run into a GitHub commit status.

An eval run's rollup (passed_count / total, as EvalRunResult exposes) becomes a
GitHub commit status on the evaluated sha: success when every case passed, else
failure, with the familiar "34/36 passed" description. The GitHub client is
injectable so it can be mocked in tests.
"""

from typing import Literal

import httpx

State = Literal["success", "failure", "pending", "error"]


def eval_state(passed_count: int, total: int) -> tuple[State, str]:
    """Map an eval rollup to a commit-status (state, description)."""

    state: State = "success" if total > 0 and passed_count == total else "failure"
    return state, f"{passed_count}/{total} passed"


class GitHubStatusReporter:
    """Posts commit statuses to the GitHub statuses API."""

    def __init__(
        self, client: httpx.AsyncClient, *, api_url: str, token: str, context: str
    ) -> None:
        self._client = client
        self._api_url = api_url.rstrip("/")
        self._token = token
        self._context = context

    async def report_eval(
        self,
        repo_full_name: str,
        sha: str,
        passed_count: int,
        total: int,
        target_url: str | None = None,
    ) -> State:
        state, description = eval_state(passed_count, total)
        payload: dict[str, str] = {
            "state": state,
            "context": self._context,
            "description": description,
        }
        if target_url:
            payload["target_url"] = target_url
        resp = await self._client.post(
            f"{self._api_url}/repos/{repo_full_name}/statuses/{sha}",
            json=payload,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        resp.raise_for_status()
        return state
