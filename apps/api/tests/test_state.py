"""Durable KV state store (#23): real Postgres round-trip + compare-and-set.

Nothing mocked -- exercises the API against the compose Postgres (the
disposable-DB conftest provisions and migrates a throwaway database per run).
"""

import uuid
from typing import Any

from agentos_api.config import get_settings
from agentos_api.sandbox_token import mint

# Scoped-sandbox-token auth matrix constants (#410).
_FAR_FUTURE = 4102444800  # 2100-01-01, valid at test time
_PAST = 1000000000  # 2001, expired at test time


def _agent(client: Any, headers: dict[str, str]) -> str:
    resp = client.post(
        "/agents",
        json={"name": "state-agent", "slack_channel": "C000000S01"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    agent_id: str = resp.json()["id"]
    return agent_id


def test_state_router_accepts_a_scoped_token_for_the_path_agent(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    # A scoped "state" token whose agent claim matches the path agent is a
    # first-class credential on the state router: no regression for the
    # platform key, and the sandboxed agent reaches its own namespace with a
    # least-privilege token instead of the raw shared key (#410).
    aid = _agent(client, auth_headers)
    api_key = get_settings().api_key
    token = mint(api_key, agent=aid, scope="state", exp=_FAR_FUTURE)
    headers = {"X-API-Key": token}
    url = f"/agents/{aid}/state/scoped/k"

    put = client.put(url, json={"value": {"n": 1}}, headers=headers)
    assert put.status_code == 200, put.text
    assert put.json()["value"] == {"n": 1}

    got = client.get(url, headers=headers)
    assert got.status_code == 200
    assert got.json()["value"] == {"n": 1}

    # The platform key still works on the same endpoint (no regression).
    assert client.get(url, headers=auth_headers).status_code == 200


def test_state_router_rejects_scoped_token_for_a_different_agent(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    other = str(uuid.uuid4())
    token = mint(get_settings().api_key, agent=other, scope="state", exp=_FAR_FUTURE)
    r = client.put(
        f"/agents/{aid}/state/scoped/k",
        json={"value": {"n": 1}},
        headers={"X-API-Key": token},
    )
    assert r.status_code == 401, r.text


def test_state_router_rejects_expired_scoped_token(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    token = mint(get_settings().api_key, agent=aid, scope="state", exp=_PAST)
    r = client.put(
        f"/agents/{aid}/state/scoped/k",
        json={"value": {"n": 1}},
        headers={"X-API-Key": token},
    )
    assert r.status_code == 401, r.text


def test_state_router_rejects_wrong_scope_token(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    token = mint(get_settings().api_key, agent=aid, scope="admin", exp=_FAR_FUTURE)
    r = client.put(
        f"/agents/{aid}/state/scoped/k",
        json={"value": {"n": 1}},
        headers={"X-API-Key": token},
    )
    assert r.status_code == 401, r.text


def test_state_router_rejects_wrong_signing_key_token(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    # Correct agent + scope, but signed with a key that is not the platform key.
    token = mint("not-the-platform-key", agent=aid, scope="state", exp=_FAR_FUTURE)
    r = client.put(
        f"/agents/{aid}/state/scoped/k",
        json={"value": {"n": 1}},
        headers={"X-API-Key": token},
    )
    assert r.status_code == 401, r.text


def test_state_router_rejects_garbage_token_without_echoing_it(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    garbage = "sbx.xxx.yyy"
    r = client.put(
        f"/agents/{aid}/state/scoped/k",
        json={"value": {"n": 1}},
        headers={"X-API-Key": garbage},
    )
    assert r.status_code == 401, r.text
    # A rejected credential is never echoed back in the error body.
    assert garbage not in r.text


def test_state_router_rejects_missing_api_key_header(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    r = client.put(f"/agents/{aid}/state/scoped/k", json={"value": {"n": 1}})
    assert r.status_code == 401, r.text


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


def test_concurrent_cas_one_writer_wins_loser_sees_compare_failure(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    # The AC's core scenario: two writers both read the same version, both try a
    # compare-and-set write; exactly one wins and the loser gets the 409.
    aid = _agent(client, auth_headers)
    url = f"/agents/{aid}/state/counter/n"

    seed = client.put(url, json={"value": {"n": 0}}, headers=auth_headers).json()
    read_version = seed["version"]  # both writers observe this version

    winner = client.put(
        url,
        json={"value": {"n": 1}, "expected_version": read_version},
        headers=auth_headers,
    )
    loser = client.put(
        url,
        json={"value": {"n": 2}, "expected_version": read_version},
        headers=auth_headers,
    )

    assert winner.status_code == 200, winner.text
    assert loser.status_code == 409, loser.text
    # The winner's write stands; the loser's did not clobber it.
    assert client.get(url, headers=auth_headers).json()["value"] == {"n": 1}


def test_append_grows_a_log_shaped_entry(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    url = f"/agents/{aid}/state/audit/log/append"

    # First append creates the entry as a single-element array (version 1).
    r1 = client.post(url, json={"item": {"event": "created"}}, headers=auth_headers)
    assert r1.status_code == 200, r1.text
    assert r1.json()["value"] == [{"event": "created"}]
    assert r1.json()["version"] == 1

    # Second append extends the array and bumps the version.
    r2 = client.post(url, json={"item": {"event": "approved"}}, headers=auth_headers)
    assert r2.json()["value"] == [{"event": "created"}, {"event": "approved"}]
    assert r2.json()["version"] == 2


def test_append_onto_a_non_array_value_is_409(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    base = f"/agents/{aid}/state/audit"
    client.put(f"{base}/obj", json={"value": {"not": "a list"}}, headers=auth_headers)

    r = client.post(f"{base}/obj/append", json={"item": 1}, headers=auth_headers)
    assert r.status_code == 409, r.text


def test_value_over_the_per_value_cap_is_rejected(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    from agentos_api.config import get_settings

    aid = _agent(client, auth_headers)
    # Shrink the per-value cap for this test. get_settings() is lru_cached, so it
    # returns a singleton we mutate and reset via cache_clear() in the finally.
    get_settings().state_max_value_bytes = 50
    try:
        oversized = {"blob": "x" * 200}
        r = client.put(
            f"/agents/{aid}/state/big/v", json={"value": oversized}, headers=auth_headers
        )
        assert r.status_code == 413, r.text
        # A small value under the cap still writes.
        ok = client.put(
            f"/agents/{aid}/state/big/v", json={"value": {"n": 1}}, headers=auth_headers
        )
        assert ok.status_code == 200
    finally:
        get_settings.cache_clear()


def test_namespace_over_the_per_namespace_cap_is_rejected(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    from agentos_api.config import get_settings

    aid = _agent(client, auth_headers)
    base = f"/agents/{aid}/state/capped"
    get_settings().state_max_namespace_bytes = 100
    try:
        # First key fits (~58 bytes serialized).
        a = client.put(f"{base}/a", json={"value": {"s": "x" * 50}}, headers=auth_headers)
        assert a.status_code == 200, a.text
        # Second key pushes the namespace total (~116 bytes) over the 100 cap.
        b = client.put(f"{base}/b", json={"value": {"s": "x" * 50}}, headers=auth_headers)
        assert b.status_code == 413, b.text
    finally:
        get_settings.cache_clear()
