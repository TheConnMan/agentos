"""POST /agents conflict handling: a duplicate is a 409, not a 500.

Real Postgres round-trip (the disposable-DB conftest provisions and migrates a
throwaway database per run); name and repo_full_name are unique columns, so a
collision must surface as a caller conflict, not an opaque server error.
"""

from typing import Any


def _create(client: Any, headers: dict[str, str], **fields: Any) -> Any:
    return client.post("/agents", json=fields, headers=headers)


def test_duplicate_name_is_409(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    first = _create(client, auth_headers, name="dup-name", slack_channel="C0AAAAAA1")
    assert first.status_code == 201, first.text

    dup = _create(client, auth_headers, name="dup-name", slack_channel="C0BBBBBB2")
    assert dup.status_code == 409, dup.text
    assert "name" in dup.json()["detail"]


def test_duplicate_repo_is_409(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    first = _create(
        client,
        auth_headers,
        name="repo-agent-a",
        slack_channel="C0CCCCCC3",
        repo_full_name="octo/shared-repo",
    )
    assert first.status_code == 201, first.text

    dup = _create(
        client,
        auth_headers,
        name="repo-agent-b",
        slack_channel="C0DDDDDD4",
        repo_full_name="octo/shared-repo",
    )
    assert dup.status_code == 409, dup.text
    assert "repository" in dup.json()["detail"]
