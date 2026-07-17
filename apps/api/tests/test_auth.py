"""Auth and health-endpoint behavior."""

import uuid
from typing import Any

from agentos_api.config import get_settings
from agentos_api.sandbox_token import mint


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


def test_bogus_console_session_cookie_is_rejected(client: Any) -> None:
    # require_api_key grew a second accepted credential (a live console session
    # cookie, ADR-0049/#630). An unrecognized cookie must not widen anything.
    client.cookies.set("agentos_console_session", "not-a-session")
    assert client.get("/agents").status_code == 401


def test_platform_key_is_honored_alongside_a_bogus_console_cookie(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    # The platform-key path is checked first and unchanged (ADR-0049): a machine
    # caller with a valid key is not broken by a junk cookie riding along.
    client.cookies.set("agentos_console_session", "not-a-session")
    assert client.get("/agents", headers=auth_headers).status_code == 200


def test_scoped_state_token_is_rejected_on_a_crud_route(client: Any) -> None:
    # A scoped sandbox "state" token authorizes the state namespace only; it must
    # be rejected by the shared require_api_key guard on every other route, so the
    # rejection is not special-cased to approvals (#410).
    token = mint(
        get_settings().api_key,
        agent=str(uuid.uuid4()),
        scope="state",
        exp=4102444800,  # 2100-01-01, valid at test time
    )
    assert client.get("/agents", headers={"X-API-Key": token}).status_code == 401
