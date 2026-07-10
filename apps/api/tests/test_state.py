"""Durable KV state store (#23): real Postgres round-trip + compare-and-set.

Nothing mocked -- exercises the API against the compose Postgres (the
disposable-DB conftest provisions and migrates a throwaway database per run).
"""

from typing import Any


def _agent(client: Any, headers: dict[str, str]) -> str:
    resp = client.post(
        "/agents",
        json={"name": "state-agent", "slack_channel": "C000000S01"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    agent_id: str = resp.json()["id"]
    return agent_id


def test_put_get_list_delete_round_trip(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    base = f"/agents/{aid}/state/approvals"

    # put (create) -> version 1
    r = client.put(
        f"{base}/thread-1", json={"value": {"status": "pending"}}, headers=auth_headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["value"] == {"status": "pending"}
    assert r.json()["version"] == 1

    # get returns what was written
    got = client.get(f"{base}/thread-1", headers=auth_headers)
    assert got.status_code == 200
    assert got.json()["value"] == {"status": "pending"}

    # put (update) -> version bumps to 2
    r2 = client.put(
        f"{base}/thread-1", json={"value": {"status": "approved"}}, headers=auth_headers
    )
    assert r2.json()["version"] == 2

    # list by namespace returns both keys
    client.put(
        f"{base}/thread-2", json={"value": {"status": "pending"}}, headers=auth_headers
    )
    listed = client.get(base, headers=auth_headers).json()
    assert {e["key"] for e in listed} == {"thread-1", "thread-2"}

    # delete -> 204, then gone
    d = client.delete(f"{base}/thread-1", headers=auth_headers)
    assert d.status_code == 204
    assert client.get(f"{base}/thread-1", headers=auth_headers).status_code == 404


def test_compare_and_set_rejects_a_stale_version(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    url = f"/agents/{aid}/state/dedupe/seen"

    v1 = client.put(url, json={"value": {"n": 1}}, headers=auth_headers).json()
    assert v1["version"] == 1

    # CAS with the current version succeeds and bumps to 2.
    ok = client.put(
        url, json={"value": {"n": 2}, "expected_version": 1}, headers=auth_headers
    )
    assert ok.status_code == 200
    assert ok.json()["version"] == 2

    # CAS with the now-stale version 1 is rejected, and the value is unchanged.
    stale = client.put(
        url, json={"value": {"n": 3}, "expected_version": 1}, headers=auth_headers
    )
    assert stale.status_code == 409, stale.text
    assert client.get(url, headers=auth_headers).json()["value"] == {"n": 2}


def test_put_unknown_agent_is_404(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    r = client.put(
        f"/agents/{missing}/state/ns/k", json={"value": {}}, headers=auth_headers
    )
    assert r.status_code == 404


def test_get_missing_entry_is_404(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    r = client.get(f"/agents/{aid}/state/ns/nope", headers=auth_headers)
    assert r.status_code == 404
