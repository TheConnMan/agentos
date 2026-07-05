"""Auth and health-endpoint behavior."""

from typing import Any


def test_health_is_open(client: Any) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_agents_require_api_key(client: Any) -> None:
    assert client.get("/agents").status_code == 401
    assert (
        client.get("/agents", headers={"X-API-Key": "wrong"}).status_code == 401
    )


def test_agents_accept_valid_key(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    assert client.get("/agents", headers=auth_headers).status_code == 200
