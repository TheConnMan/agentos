"""Agent memory read + learned-from trace-back (#266).

Seeds the runner-written memory log via the state append endpoint (the same wire
the runner uses), then exercises the read/trace-back surface against the real
compose Postgres.
"""

from typing import Any


def _agent(client: Any, headers: dict[str, str]) -> str:
    resp = client.post(
        "/agents",
        json={"name": "memory-agent", "slack_channel": "C000000M01"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    agent_id: str = resp.json()["id"]
    return agent_id


def _remember(client: Any, headers: dict[str, str], aid: str, record: dict) -> None:
    """Append one {content, provenance} record to the memory log key."""
    url = f"/agents/{aid}/state/memory/log/append"
    r = client.post(url, json={"item": record}, headers=headers)
    assert r.status_code == 200, r.text


def test_list_memory_empty_for_fresh_agent(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    r = client.get(f"/agents/{aid}/memory", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json() == []


def test_list_memory_returns_entries_with_provenance(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    _remember(
        client,
        auth_headers,
        aid,
        {
            "content": "deploy is a git push",
            "provenance": {
                "learned_from_session_id": "sess-1",
                "source_trace_ids": ["trace-a", "trace-b"],
                "recorded_at": "2026-07-13T00:00:00+00:00",
            },
        },
    )
    _remember(client, auth_headers, aid, {"content": "no-provenance lesson"})

    entries = client.get(f"/agents/{aid}/memory", headers=auth_headers).json()
    assert len(entries) == 2
    assert entries[0]["index"] == 0
    assert entries[0]["content"] == "deploy is a git push"
    assert entries[0]["provenance"]["source_trace_ids"] == ["trace-a", "trace-b"]
    # A record with no recorded provenance degrades to empty provenance, not 500.
    assert entries[1]["provenance"]["source_trace_ids"] == []


def test_trace_back_resolves_sessions_and_traces(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    _remember(
        client,
        auth_headers,
        aid,
        {
            "content": "prod push reuses the dev bundle",
            "provenance": {
                "learned_from_session_id": "sess-9",
                "source_trace_ids": ["trace-x", "trace-y"],
                "recorded_at": "2026-07-13T01:00:00+00:00",
            },
        },
    )

    r = client.get(f"/agents/{aid}/memory/0/provenance", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["learned_from_session_id"] == "sess-9"
    assert body["recorded_at"] == "2026-07-13T01:00:00+00:00"
    trace_ids = [t["trace_id"] for t in body["source_traces"]]
    assert trace_ids == ["trace-x", "trace-y"]
    # Each source trace resolves to a viewable link.
    for t in body["source_traces"]:
        assert t["trace_url"].endswith(f"/trace/{t['trace_id']}")


def test_trace_back_out_of_range_index_is_404(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    _remember(client, auth_headers, aid, {"content": "only one"})
    r = client.get(f"/agents/{aid}/memory/5/provenance", headers=auth_headers)
    assert r.status_code == 404, r.text


def test_memory_unknown_agent_is_404(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    r = client.get(f"/agents/{missing}/memory", headers=auth_headers)
    assert r.status_code == 404, r.text


def _prov(session_id: str, *traces: str) -> dict:
    return {
        "learned_from_session_id": session_id,
        "source_trace_ids": list(traces),
        "recorded_at": "2026-07-13T00:00:00+00:00",
    }


def test_edit_entry_preserves_provenance(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    _remember(
        client,
        auth_headers,
        aid,
        {"content": "old lesson", "provenance": _prov("sess-9", "t-x", "t-y")},
    )

    r = client.put(
        f"/agents/{aid}/memory/0",
        json={"content": "corrected lesson"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["content"] == "corrected lesson"
    # Editing the text must not erase where it was learned from.
    assert r.json()["provenance"]["source_trace_ids"] == ["t-x", "t-y"]

    # The change is durable -- a fresh read (what the next boot sees) reflects it.
    entries = client.get(f"/agents/{aid}/memory", headers=auth_headers).json()
    assert entries[0]["content"] == "corrected lesson"
    assert entries[0]["provenance"]["learned_from_session_id"] == "sess-9"


def test_delete_entry_removes_one_and_reindexes(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    for c in ("a", "b", "c"):
        _remember(client, auth_headers, aid, {"content": c})

    d = client.delete(f"/agents/{aid}/memory/1", headers=auth_headers)
    assert d.status_code == 204, d.text

    entries = client.get(f"/agents/{aid}/memory", headers=auth_headers).json()
    assert [e["content"] for e in entries] == ["a", "c"]
    assert [e["index"] for e in entries] == [0, 1]


def test_edit_out_of_range_is_404(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    _remember(client, auth_headers, aid, {"content": "only one"})
    r = client.put(
        f"/agents/{aid}/memory/5", json={"content": "x"}, headers=auth_headers
    )
    assert r.status_code == 404, r.text


def test_delete_out_of_range_is_404(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    _remember(client, auth_headers, aid, {"content": "only one"})
    r = client.delete(f"/agents/{aid}/memory/9", headers=auth_headers)
    assert r.status_code == 404, r.text
