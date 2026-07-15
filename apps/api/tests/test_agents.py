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


def test_agent_approval_required_tools_round_trip(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    # Create with permission gates (#245); they come back on reads.
    created = client.post(
        "/agents",
        json={
            "name": "gated-agent",
            "slack_channel": "C000000G01",
            "approval_required_tools": ["Bash", "mcp__github__create_issue"],
        },
        headers=auth_headers,
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["approval_required_tools"] == ["Bash", "mcp__github__create_issue"]

    # PATCH replaces the set; an explicit empty list clears it (NULL posture).
    patched = client.patch(
        f"/agents/{body['id']}",
        json={"approval_required_tools": ["WebFetch"]},
        headers=auth_headers,
    )
    assert patched.status_code == 200
    assert patched.json()["approval_required_tools"] == ["WebFetch"]

    cleared = client.patch(
        f"/agents/{body['id']}",
        json={"approval_required_tools": []},
        headers=auth_headers,
    )
    assert cleared.json()["approval_required_tools"] is None

    # Omitting the field leaves the gates unchanged.
    repatched = client.patch(
        f"/agents/{body['id']}",
        json={"approval_required_tools": ["Bash"]},
        headers=auth_headers,
    )
    assert repatched.json()["approval_required_tools"] == ["Bash"]
    untouched = client.patch(
        f"/agents/{body['id']}", json={"model": "claude-sonnet-5"}, headers=auth_headers
    )
    assert untouched.json()["approval_required_tools"] == ["Bash"]


def test_agent_approval_required_tools_rejects_bad_names(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    # A comma inside a name would split into two wrong gates on the env wire.
    for bad in (["Bash,Read"], [""], ["  "]):
        resp = client.post(
            "/agents",
            json={
                "name": f"bad-{bad[0].strip() or 'blank'}",
                "slack_channel": "C000000G02",
                "approval_required_tools": bad,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 422, resp.text
