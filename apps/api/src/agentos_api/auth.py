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

from . import crud
from .config import get_settings
from .deps import SessionDep

API_KEY_HEADER = "X-API-Key"

# The console's session cookie. Named here because it is a credential the shared
# dependency accepts; the console router sets and clears it.
CONSOLE_SESSION_COOKIE = "agentos_console_session"


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
) -> None:
    """The platform key (machine callers) OR a live console session cookie.

    Order is load-bearing: the platform-key path is checked first and returns
    before the session is ever used, so the worker, runner, and CLI still pay no
    database read. A console session costs one indexed read per request, which
    is what makes revocation and expiry take effect immediately rather than when
    a cached decision happens to lapse.
    """

    if verify_platform_key(x_api_key):
        return
    if agentos_console_session and await crud.live_console_session(
        session, agentos_console_session
    ):
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="missing or invalid API key",
    )
