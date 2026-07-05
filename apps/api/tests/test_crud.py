"""CRUD round-trip against the real compose Postgres.

create agent -> create version -> deploy to dev -> list/get, the B1 done-when.
"""

from typing import Any


def test_full_round_trip(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    # create agent
    resp = client.post(
        "/agents",
        json={"name": "triage-bot", "slack_channel": "#triage"},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    agent = resp.json()
    agent_id = agent["id"]
    assert agent["name"] == "triage-bot"

    # create version
    resp = client.post(
        f"/agents/{agent_id}/versions",
        json={"version_label": "v1", "created_by": "bconn"},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    version = resp.json()
    version_id = version["id"]
    assert version["bundle_ref"] is None
    assert version["agent_id"] == agent_id

    # deploy to dev
    resp = client.post(
        "/deployments",
        json={
            "agent_id": agent_id,
            "version_id": version_id,
            "environment": "dev",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    deployment = resp.json()
    deployment_id = deployment["id"]
    assert deployment["environment"] == "dev"
    assert deployment["status"] == "active"

    # list + get every resource
    listed_agents = client.get("/agents", headers=auth_headers).json()
    assert [a["id"] for a in listed_agents] == [agent_id]

    got_agent = client.get(f"/agents/{agent_id}", headers=auth_headers)
    assert got_agent.status_code == 200
    assert got_agent.json()["slack_channel"] == "#triage"

    listed_versions = client.get(
        f"/agents/{agent_id}/versions", headers=auth_headers
    ).json()
    assert [v["id"] for v in listed_versions] == [version_id]

    listed_deployments = client.get(
        "/deployments", params={"agent_id": agent_id}, headers=auth_headers
    ).json()
    assert [d["id"] for d in listed_deployments] == [deployment_id]

    got_deployment = client.get(
        f"/deployments/{deployment_id}", headers=auth_headers
    )
    assert got_deployment.status_code == 200
    assert got_deployment.json()["version_id"] == version_id


def test_missing_agent_returns_404(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    assert (
        client.get(f"/agents/{missing}", headers=auth_headers).status_code == 404
    )


def test_version_for_missing_agent_returns_404(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    resp = client.post(
        f"/agents/{missing}/versions",
        json={"version_label": "v1", "created_by": "bconn"},
        headers=auth_headers,
    )
    assert resp.status_code == 404
