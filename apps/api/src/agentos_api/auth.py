"""Shared authentication for every router.

MVP auth is one shared key delivered in the `X-API-Key` header and compared
against Settings.api_key. J1 replaces this with GitHub-App-scoped identities.

`require_api_key` also accepts a live console session cookie (#630, ADR-0049),
so the browser never has to hold the platform key. That is an extension of the
one shared dependency rather than a second auth scheme on a router, which is
the boundary apps/api/CLAUDE.md draws.
"""

import hmac
from typing import Annotated

from fastapi import Cookie, Header, HTTPException, status
from sqlalchemy.exc import SQLAlchemyError

from . import crud
from .config import get_settings
from .deps import SessionDep

API_KEY_HEADER = "X-API-Key"

# The console's session cookie. Named here because it is a credential the shared
# dependency accepts; the console router sets and clears it.
CONSOLE_SESSION_COOKIE = "agentos_console_session"

# The header that must accompany the cookie on the cookie path, and the CSRF
# control the cookie itself cannot provide (ADR-0049).
#
# A cookie is ambient authority: the browser attaches it to a cross-origin form
# post whether or not the page that forged the request has any business with
# this API. `SameSite=Strict` does not close that, because "site" ignores the
# port: the ADR's own prescribed login path is http://localhost:8080, which is
# same-site with every other localhost port -- any dev server, any hostile
# package's server. A body-less POST to /agents/{id}/kill from such a page is a
# CORS-simple request, so nothing preflights it and the cookie rides along.
#
# A custom request header is not settable cross-origin without a preflight, and
# this API deliberately runs no CORS middleware, so the preflight fails and the
# forged request never reaches the route. This is the OWASP custom-request-header
# defense, and it restores the structural immunity the `X-API-Key` header
# credential had before the cookie existed. Any value is accepted: the header's
# presence is the whole proof, because being able to set it at all is what a
# cross-origin attacker cannot do.
CONSOLE_SESSION_HEADER = "X-Console-Session"

SESSION_STORE_DOWN_BODY = {
    "error": (
        "cannot verify the console session: the session store is unreachable"
    ),
    "fix": (
        "Use the CLI, which authenticates with the platform key and needs no "
        "session: agentos cluster status, agentos cluster agents. The console "
        "cannot authenticate while the database is down."
    ),
}


class ConsoleSessionStoreUnavailable(Exception):
    """The cookie path could not reach the session store.

    Raised by `require_api_key` and rendered as a 503 by the handler
    `main.create_app` registers. It is not an HTTPException because the
    `{error, fix}` shape here is the flat one the console's other failure
    (the exchange's insecure-origin 400) already returns, and FastAPI's
    HTTPException handler would nest it under `detail`.

    It must not collapse into the 401: telling an operator their session is bad
    when Postgres is merely down sends them to log in again, which cannot work
    either, instead of to the CLI that does.
    """


def verify_platform_key(x_api_key: str | None) -> bool:
    """True when the header carries the shared platform API key (constant-time).

    The single place that defines what 'the platform key' means, shared by
    require_api_key (raise on fail) and the state router's require_state_access
    (fall through to the scoped-token check)."""
    if x_api_key is None:
        return False
    return hmac.compare_digest(x_api_key, get_settings().api_key)


async def require_platform_key(
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    """The platform key and nothing else: `require_api_key` minus the console
    cookie.

    For the console's own operator surface (#630, ADR-0049), which is the CLI's
    to call, not the browser's. A console session must not be able to mint
    itself a fresh login code -- that would be the refresh token the ADR rules
    out, quietly defeating the fixed absolute session lifetime -- nor enumerate
    or mass-revoke the session store it is merely a member of.
    """

    if not verify_platform_key(x_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid API key",
        )


async def require_api_key(
    session: SessionDep,
    x_api_key: Annotated[str | None, Header()] = None,
    agentos_console_session: Annotated[str | None, Cookie()] = None,
    x_console_session: Annotated[str | None, Header()] = None,
) -> None:
    """The platform key (machine callers) OR a live console session cookie.

    Order is load-bearing: the platform-key path is checked first and returns
    before the session or the CSRF header is ever looked at, so the worker,
    runner, and CLI still pay no database read and are never asked for a header
    a browser-only defense exists to require. A console session costs one
    indexed read per request, which is what makes revocation and expiry take
    effect immediately rather than when a cached decision happens to lapse.

    The cookie path additionally requires `X-Console-Session`
    (CONSOLE_SESSION_HEADER): see that constant for why the cookie alone is
    CSRF-able even with SameSite=Strict.
    """

    if verify_platform_key(x_api_key):
        return
    if agentos_console_session and x_console_session is not None:
        try:
            live = await crud.live_console_session(session, agentos_console_session)
        except SQLAlchemyError as exc:
            # The kill switch and the pod-log proxy are what an operator reaches
            # for when the system is sick, and neither needed Postgres to
            # authenticate before the cookie existed. An opaque 500 there is the
            # worst possible moment for one.
            raise ConsoleSessionStoreUnavailable() from exc
        if live:
            return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="missing or invalid API key",
    )
