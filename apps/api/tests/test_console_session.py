"""Console sessions and CLI-minted login codes (ADR-0049, #630).

Real Postgres (the conftest's disposable per-run database), nothing internal
mocked. The console's credential is a server-managed, revocable session cookie
established by exchanging a single-use login code that the CLI mints under the
platform key. These tests pin the outcomes that make that stronger than the
status quo: browser code never sees the platform key, the cookie (with the
``X-Console-Session`` CSRF header) authorizes a real protected route, a forged
cross-site request carrying the cookie does not, only hashes reach the database,
and revocation is immediate.

Two deliberate test-side choices:

* The TestClient runs on an ``https://`` base URL. The exchange sets a
  ``Secure`` cookie, and a standards-conformant cookie jar (httpx's, and every
  browser's) refuses to store a ``Secure`` cookie received over plaintext. An
  ``http://testserver`` client would silently drop the cookie and every
  assertion below would be testing nothing.
* Expiry is driven by ageing the stored rows rather than sleeping, and the
  ageing helper discovers the expiry columns from ``information_schema`` rather
  than hardcoding their names, so these tests pin the behavior without dictating
  the column spelling.
"""

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from typing import Any

import pytest
from agentos_api.config import get_settings
from agentos_api.main import create_app
from agentos_api.sandbox_token import mint
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import create_async_engine

COOKIE_NAME = "agentos_console_session"
TABLE = "agentos.console_sessions"

# The CSRF header the cookie path requires (ADR-0049). Sent explicitly on every
# cookie-authenticated call below rather than defaulted onto the client, so that
# a test asserting 401 is visibly asserting "the session is dead", not silently
# passing because the header went missing.
CONSOLE_HEADER = {"X-Console-Session": "1"}

# A secure-context origin a real console is served from, and a plaintext
# NodePort origin, which is the exact case the exchange must refuse.
HTTPS_ORIGIN = "https://console.example"
NODEPORT_ORIGIN = "http://10.1.2.3:30000"


@pytest.fixture
def console_client(_disposable_db: Any) -> Any:
    """A TestClient on an https origin, so the Secure session cookie is kept."""

    with TestClient(create_app(), base_url="https://testserver") as test_client:
        yield test_client


def _run(coro_factory: Any) -> Any:
    async def _main() -> Any:
        engine = create_async_engine(get_settings().database_url)
        try:
            return await coro_factory(engine)
        finally:
            await engine.dispose()

    return asyncio.run(_main())


@pytest.fixture
def clean_console_sessions(migrated: None) -> None:
    """Row counts and revocation totals below are exact, so each test starts
    from an empty table (the conftest's clean_db truncates the agent tables
    only)."""

    async def _truncate(engine: Any) -> None:
        async with engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {TABLE}"))

    _run(_truncate)


def _rows() -> list[dict[str, Any]]:
    async def _select(engine: Any) -> list[dict[str, Any]]:
        async with engine.connect() as conn:
            result = await conn.execute(text(f"select * from {TABLE}"))
            return [dict(row) for row in result.mappings().all()]

    return _run(_select)


def _expire_all_sessions() -> int:
    """Force every expiry on every stored session to an elapsed timestamp.

    Sets an ABSOLUTE past timestamp rather than subtracting an interval: a
    relative shift has to out-run whichever TTL produced the value, so it would
    silently leave a 12-hour session live and the test would pass or fail for a
    reason that has nothing to do with expiry.

    Discovers the expiry columns from the catalog instead of naming them, so
    these tests pin "an elapsed expiry is refused" rather than a column
    spelling. Returns the number of rows expired.
    """

    async def _expire(engine: Any) -> int:
        async with engine.begin() as conn:
            found = await conn.execute(
                text(
                    "select column_name from information_schema.columns "
                    "where table_schema = 'agentos' "
                    "and table_name = 'console_sessions' "
                    "and column_name like '%expires%'"
                )
            )
            cols = list(found.scalars().all())
            assert cols, f"{TABLE} stores no expiry column to age"
            assignments = ", ".join(
                f"{col} = now() - interval '1 hour'" for col in cols
            )
            expired = await conn.execute(text(f"update {TABLE} set {assignments}"))
            return int(expired.rowcount)

    return _run(_expire)


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _mint_code(client: Any, auth_headers: dict[str, str]) -> dict[str, Any]:
    resp = client.post("/console/login-codes", headers=auth_headers, json={})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _exchange(client: Any, code: str, origin: str = HTTPS_ORIGIN) -> Any:
    return client.post(
        "/console/session", json={"code": code}, headers={"Origin": origin}
    )


def _cookie_value(resp: Any) -> str:
    jar = SimpleCookie()
    jar.load(resp.headers["set-cookie"])
    return jar[COOKIE_NAME].value


def _login(client: Any, auth_headers: dict[str, str]) -> str:
    """Full front-door flow; returns the raw session token the cookie carries."""

    minted = _mint_code(client, auth_headers)
    resp = _exchange(client, minted["code"])
    assert resp.status_code == 200, resp.text
    return _cookie_value(resp)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def test_minting_a_login_code_requires_the_platform_key(
    console_client: Any, clean_console_sessions: None
) -> None:
    # The mint endpoint is the CLI's, not the browser's: an unauthenticated
    # caller must not be able to hand itself a code and log in.
    assert console_client.post("/console/login-codes", json={}).status_code == 401
    assert (
        console_client.post(
            "/console/login-codes", json={}, headers={"X-API-Key": "wrong"}
        ).status_code
        == 401
    )
    assert _rows() == []


def test_minted_code_carries_an_expiry_from_the_configured_ttl(
    console_client: Any, auth_headers: dict[str, str], clean_console_sessions: None
) -> None:
    before = datetime.now(UTC)
    minted = _mint_code(console_client, auth_headers)

    ttl = get_settings().console_login_code_ttl_seconds
    assert ttl == 600  # the shipped default the ADR specifies
    delta = (_parse_ts(minted["expires_at"]) - before).total_seconds()
    assert ttl - 60 < delta <= ttl + 5
    assert uuid.UUID(str(minted["session_id"]))
    assert minted["code"]


def test_session_cookie_authorizes_a_protected_route_with_no_platform_key(
    console_client: Any, auth_headers: dict[str, str], clean_console_sessions: None
) -> None:
    # The point of the whole design: the browser holds no platform key, and the
    # cookie it does hold is a real credential on a real router.
    assert console_client.get("/agents", headers=CONSOLE_HEADER).status_code == 401

    _login(console_client, auth_headers)

    resp = console_client.get("/agents", headers=CONSOLE_HEADER)
    assert resp.status_code == 200, resp.text
    assert "X-API-Key" not in resp.request.headers
    assert COOKIE_NAME in console_client.cookies


def test_set_cookie_carries_the_hardening_attributes(
    console_client: Any, auth_headers: dict[str, str], clean_console_sessions: None
) -> None:
    # HttpOnly is what makes this strictly stronger than the status quo (page
    # script cannot read the credential); SameSite=Strict is the CSRF control.
    minted = _mint_code(console_client, auth_headers)
    resp = _exchange(console_client, minted["code"])
    assert resp.status_code == 200, resp.text

    raw = resp.headers["set-cookie"]
    assert raw.startswith(f"{COOKIE_NAME}=")
    lowered = raw.lower()
    assert "httponly" in lowered
    assert "secure" in lowered
    assert "samesite=strict" in lowered
    assert "path=/" in lowered


def test_login_code_is_single_use(
    console_client: Any, auth_headers: dict[str, str], clean_console_sessions: None
) -> None:
    minted = _mint_code(console_client, auth_headers)
    first = _exchange(console_client, minted["code"])
    assert first.status_code == 200, first.text
    first_token = _cookie_value(first)

    # A replayed code buys nothing...
    replay = _exchange(console_client, minted["code"])
    assert replay.status_code == 401

    # ...and the refusal did not collaterally kill the session it minted.
    assert console_client.cookies[COOKIE_NAME] == first_token
    assert console_client.get("/agents", headers=CONSOLE_HEADER).status_code == 200


def test_unknown_login_code_is_rejected(
    console_client: Any, auth_headers: dict[str, str], clean_console_sessions: None
) -> None:
    assert _exchange(console_client, "not-a-real-code").status_code == 401
    assert COOKIE_NAME not in console_client.cookies


def test_expired_login_code_is_rejected(
    console_client: Any, auth_headers: dict[str, str], clean_console_sessions: None
) -> None:
    minted = _mint_code(console_client, auth_headers)
    assert _expire_all_sessions() == 1

    assert _exchange(console_client, minted["code"]).status_code == 401
    assert COOKIE_NAME not in console_client.cookies
    assert console_client.get("/agents", headers=CONSOLE_HEADER).status_code == 401


def test_expired_session_cookie_is_rejected_on_a_protected_route(
    console_client: Any, auth_headers: dict[str, str], clean_console_sessions: None
) -> None:
    _login(console_client, auth_headers)
    assert console_client.get("/agents", headers=CONSOLE_HEADER).status_code == 200

    assert _expire_all_sessions() == 1

    # Fixed absolute lifetime, no sliding refresh (ADR-0049).
    assert console_client.get("/agents", headers=CONSOLE_HEADER).status_code == 401
    status = console_client.get("/console/session")
    assert status.status_code == 200
    assert status.json() == {"authenticated": False, "expires_at": None}


def test_get_session_reports_the_live_session(
    console_client: Any, auth_headers: dict[str, str], clean_console_sessions: None
) -> None:
    anonymous = console_client.get("/console/session")
    assert anonymous.status_code == 200
    assert anonymous.json() == {"authenticated": False, "expires_at": None}

    minted = _mint_code(console_client, auth_headers)
    exchanged = _exchange(console_client, minted["code"])
    assert exchanged.status_code == 200, exchanged.text

    live = console_client.get("/console/session")
    assert live.status_code == 200
    body = live.json()
    assert body["authenticated"] is True
    assert _parse_ts(body["expires_at"]) == _parse_ts(exchanged.json()["expires_at"])

    ttl = get_settings().console_session_ttl_seconds
    assert ttl == 43200  # the shipped default the ADR specifies
    delta = (_parse_ts(body["expires_at"]) - datetime.now(UTC)).total_seconds()
    assert ttl - 60 < delta <= ttl + 5


def test_self_logout_revokes_the_session_and_is_idempotent(
    console_client: Any, auth_headers: dict[str, str], clean_console_sessions: None
) -> None:
    _login(console_client, auth_headers)
    assert console_client.get("/agents", headers=CONSOLE_HEADER).status_code == 200

    out = console_client.delete("/console/session")
    assert out.status_code == 204
    assert out.content == b""
    # The cookie is cleared client-side...
    assert COOKIE_NAME not in console_client.cookies
    assert console_client.get("/agents", headers=CONSOLE_HEADER).status_code == 401
    assert console_client.get("/console/session").json()["authenticated"] is False

    # ...and server-side, so a copy of the cookie taken before logout is dead.
    # (Clearing only the browser's copy would leave a stolen token live.)
    assert console_client.delete("/console/session").status_code == 204


def test_self_logout_kills_a_stolen_copy_of_the_cookie(
    console_client: Any, auth_headers: dict[str, str], clean_console_sessions: None
) -> None:
    token = _login(console_client, auth_headers)
    stolen = {"Cookie": f"{COOKIE_NAME}={token}", **CONSOLE_HEADER}
    assert console_client.get("/agents", headers=CONSOLE_HEADER).status_code == 200

    assert console_client.delete("/console/session").status_code == 204

    # Revocation is a durable row write, not a cookie deletion.
    assert console_client.get("/agents", headers=stolen).status_code == 401


def test_revoking_all_sessions_kills_a_live_cookie_immediately(
    console_client: Any, auth_headers: dict[str, str], clean_console_sessions: None
) -> None:
    other = TestClient(create_app(), base_url="https://testserver")
    with other:
        _login(console_client, auth_headers)
        _login(other, auth_headers)
        assert console_client.get("/agents", headers=CONSOLE_HEADER).status_code == 200
        assert other.get("/agents", headers=CONSOLE_HEADER).status_code == 200

        assert console_client.delete("/console/sessions").status_code == 401
        assert (
            console_client.delete(
                "/console/sessions", headers={"X-API-Key": "wrong"}
            ).status_code
            == 401
        )

        killed = console_client.delete("/console/sessions", headers=auth_headers)
        assert killed.status_code == 200, killed.text
        assert killed.json() == {"revoked": 2}

        assert console_client.get("/agents", headers=CONSOLE_HEADER).status_code == 401
        assert other.get("/agents", headers=CONSOLE_HEADER).status_code == 401

        # Only LIVE sessions count, so a second sweep revokes nothing.
        again = console_client.delete("/console/sessions", headers=auth_headers)
        assert again.json() == {"revoked": 0}


def test_only_hashes_of_the_code_and_token_are_stored(
    console_client: Any, auth_headers: dict[str, str], clean_console_sessions: None
) -> None:
    minted = _mint_code(console_client, auth_headers)
    exchanged = _exchange(console_client, minted["code"])
    assert exchanged.status_code == 200, exchanged.text
    token = _cookie_value(exchanged)
    code = minted["code"]

    rows = _rows()
    assert len(rows) == 1
    stored = {str(value) for value in rows[0].values()}

    # A database read must not replay a session (ADR-0049): the raw values are
    # absent and their SHA-256 digests are what is on disk.
    assert code not in stored
    assert token not in stored
    assert _sha256(code) in stored
    assert _sha256(token) in stored


@pytest.mark.parametrize(
    "origin",
    [
        HTTPS_ORIGIN,
        "https://agentos.internal:8443",
        "http://localhost:5173",
        "http://127.0.0.1:4273",
        "http://[::1]:8080",
    ],
)
def test_exchange_allows_a_secure_context_origin(
    console_client: Any,
    auth_headers: dict[str, str],
    clean_console_sessions: None,
    origin: str,
) -> None:
    # Browsers treat https and loopback as secure contexts and honor Secure
    # cookies for both, so both must be able to log in.
    minted = _mint_code(console_client, auth_headers)
    resp = _exchange(console_client, minted["code"], origin=origin)
    assert resp.status_code == 200, resp.text
    assert _cookie_value(resp)


@pytest.mark.parametrize(
    "origin",
    [NODEPORT_ORIGIN, "http://agentos.internal:30080"],
)
def test_exchange_refuses_a_plaintext_origin_with_a_fix(
    console_client: Any,
    auth_headers: dict[str, str],
    clean_console_sessions: None,
    origin: str,
) -> None:
    # Fail-closed: a Secure cookie set over plaintext is silently unprotected,
    # so the exchange refuses and hands back the instruction that fixes it.
    minted = _mint_code(console_client, auth_headers)
    resp = _exchange(console_client, minted["code"], origin=origin)
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body.get("error")
    assert "port-forward" in body.get("fix", "")
    assert COOKIE_NAME not in console_client.cookies

    # A refused exchange must not have burned the code: the operator can retry
    # over the port-forward the fix told them to open.
    retry = _exchange(console_client, minted["code"])
    assert retry.status_code == 200, retry.text


def test_exchange_refuses_a_missing_origin(
    console_client: Any, auth_headers: dict[str, str], clean_console_sessions: None
) -> None:
    minted = _mint_code(console_client, auth_headers)
    resp = console_client.post("/console/session", json={"code": minted["code"]})
    assert resp.status_code == 400, resp.text
    assert resp.json().get("error")
    assert COOKIE_NAME not in console_client.cookies


def test_scoped_state_token_is_not_accepted_as_a_console_session(
    console_client: Any, clean_console_sessions: None
) -> None:
    # A sandbox `state` token (ADR-0033) is scoped to one agent's state
    # namespace. The console cookie is a second credential on the shared
    # require_api_key dependency; it must not become a laundering path that
    # promotes a scoped token to platform-wide access.
    token = mint(
        get_settings().api_key,
        agent=str(uuid.uuid4()),
        scope="state",
        exp=4102444800,  # 2100-01-01, valid at test time
    )
    assert (
        console_client.get(
            "/agents", headers={"Cookie": f"{COOKIE_NAME}={token}", **CONSOLE_HEADER}
        ).status_code
        == 401
    )
    assert (
        console_client.get("/agents", headers={"X-API-Key": token}).status_code == 401
    )


def test_cookie_without_the_csrf_header_cannot_kill_an_agent(
    console_client: Any, auth_headers: dict[str, str], clean_console_sessions: None
) -> None:
    """The CSRF regression test, shaped like the real attack (ADR-0049).

    Before the session cookie existed, the console's authority was an
    ``X-API-Key`` header, which is structurally CSRF-immune: a cross-origin page
    cannot set it without a preflight. A cookie is ambient, and SameSite=Strict
    does not save it -- "site" ignores the port, so the ADR's own prescribed
    ``http://localhost:8080`` login path is same-site with every other localhost
    port. This is the exact request such a page can auto-submit: a body-less
    cross-origin form POST to the kill switch, which is a CORS-SIMPLE request, so
    nothing preflights it and the absent CORS middleware rejects nothing. The
    cookie rides along; only the missing custom header stops it.
    """

    agent = console_client.post(
        "/agents",
        headers=auth_headers,
        json={"name": "csrf-target", "slack_channel": "C0123ABCDEF"},
    )
    assert agent.status_code == 201, agent.text
    agent_id = agent.json()["id"]

    _login(console_client, auth_headers)
    # The session is live and CAN kill when it asks properly...
    assert (
        console_client.get(f"/agents/{agent_id}/kill", headers=CONSOLE_HEADER).status_code
        == 200
    )

    # ...but the forged request, which carries the cookie and nothing else, is
    # refused. A cross-origin page cannot add the header without a preflight this
    # CORS-free API fails.
    forged = console_client.post(
        f"/agents/{agent_id}/kill",
        headers={"Origin": "http://evil.localhost:1234"},
    )
    assert forged.status_code == 401, forged.text
    assert COOKIE_NAME in console_client.cookies  # the cookie WAS sent

    # The kill switch did not fire.
    state = console_client.get(f"/agents/{agent_id}/kill", headers=CONSOLE_HEADER)
    assert state.status_code == 200, state.text
    assert state.json() == {"killed": False}


def test_cookie_with_the_csrf_header_still_authorizes(
    console_client: Any, auth_headers: dict[str, str], clean_console_sessions: None
) -> None:
    # The header's PRESENCE is the whole proof: being able to set it at all is
    # what a cross-origin attacker cannot do, so its value is not a secret and
    # is not checked.
    _login(console_client, auth_headers)

    assert console_client.get("/agents", headers=CONSOLE_HEADER).status_code == 200
    assert (
        console_client.get(
            "/agents", headers={"X-Console-Session": "anything"}
        ).status_code
        == 200
    )
    assert console_client.get("/agents").status_code == 401


def test_platform_key_needs_no_csrf_header_and_no_session(
    console_client: Any, auth_headers: dict[str, str], clean_console_sessions: None
) -> None:
    # The CSRF header is a browser-only defense. The worker, runner, and CLI
    # authenticate with the platform key alone and must never be asked for it --
    # requiring it of them would buy nothing and break every existing caller.
    resp = console_client.get("/agents", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert "X-Console-Session" not in resp.request.headers
    assert COOKIE_NAME not in console_client.cookies
    assert _rows() == []  # the platform-key path read no session


def test_session_store_being_down_is_a_503_naming_the_cli_not_a_500(
    console_client: Any,
    auth_headers: dict[str, str],
    clean_console_sessions: None,
    monkeypatch: Any,
) -> None:
    """A database outage must not turn the kill switch opaque (ADR-0049).

    The kill switch and the pod-log proxy are what an operator reaches for when
    the system is already sick, and neither needed Postgres to authenticate
    before the cookie existed. A 500 there is the worst possible moment for one,
    and a 401 would be a lie that sends the operator to log in again -- which
    also cannot work while the store is down.
    """

    from agentos_api import auth as auth_module

    _login(console_client, auth_headers)
    assert console_client.get("/agents", headers=CONSOLE_HEADER).status_code == 200

    async def _down(*_args: Any, **_kwargs: Any) -> None:
        raise OperationalError("select 1", {}, Exception("connection refused"))

    monkeypatch.setattr(auth_module.crud, "live_console_session", _down)

    resp = console_client.get("/agents", headers=CONSOLE_HEADER)
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body.get("error")
    # The fix is the CLI: it holds the platform key and needs no session.
    assert "CLI" in body.get("fix", "")

    # The platform key is unaffected: it returns before the store is ever read,
    # so a machine caller still works while the console cannot.
    assert console_client.get("/agents", headers=auth_headers).status_code == 200
