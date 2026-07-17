"""Console login: CLI-minted login codes and the session cookie they buy
(#630, ADR-0049).

The console used to authenticate by sending the shared platform key from browser
JavaScript, which meant the key travelled in a URL, browser history, the Referer
header, and proxy logs, and authorized every router. Here the browser never sees
it: the CLI mints a short-lived single-use login code under the platform key, and
the console exchanges that code for an HttpOnly session cookie that page script
cannot read and an operator can revoke with a row write.

The routes split by who calls them. Minting and the operator's inventory are the
CLI's, under the platform key. The exchange and the session's own
status/logout are the browser's, and carry no auth dependency because the cookie
is the thing they are establishing or reading.
"""

from typing import Annotated, Any
from urllib.parse import urlparse

from fastapi import APIRouter, Cookie, Depends, Header, Response, status
from fastapi.responses import JSONResponse

from .. import crud
from ..auth import CONSOLE_SESSION_COOKIE, require_platform_key
from ..config import get_settings
from ..deps import SessionDep
from ..schemas import (
    ConsoleLoginCodeMint,
    ConsoleLoginCodeOut,
    ConsoleRevokeOut,
    ConsoleSessionExchange,
    ConsoleSessionListItem,
    ConsoleSessionOut,
    ConsoleSessionStatus,
)

router = APIRouter(prefix="/console", tags=["console"])

# Hosts a browser treats as a secure context over plaintext, and therefore for
# which it honors a Secure cookie. Everything else must be https.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

_INSECURE_ORIGIN_FIX = (
    "Reach the console over a secure context and log in again: "
    "kubectl port-forward -n agentos svc/agentos-ui 8080:80 "
    "then open http://localhost:8080. A plaintext origin (a NodePort URL) "
    "cannot hold the session cookie."
)


def _is_secure_context(origin: str | None) -> bool:
    """Whether a browser at this origin would treat itself as a secure context.

    Fail-closed and by construction: a missing, empty, or unparseable Origin is
    not a secure context, so the exchange refuses rather than establishing a
    session over a channel that does not protect it.
    """

    if not origin:
        return False
    parsed = urlparse(origin)
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and parsed.hostname in _LOOPBACK_HOSTS


@router.post(
    "/login-codes",
    response_model=ConsoleLoginCodeOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_platform_key)],
)
async def mint_login_code(
    data: ConsoleLoginCodeMint, session: SessionDep
) -> ConsoleLoginCodeOut:
    """Mint a login code for an operator to paste into the console.

    Under the platform key on purpose: this is the CLI's endpoint, not the
    browser's. An unauthenticated caller that could mint its own code could log
    itself in, which would make the whole exchange decorative.
    """

    settings = get_settings()
    code, row = await crud.mint_console_login_code(
        session, data.label, settings.console_login_code_ttl_seconds
    )
    return ConsoleLoginCodeOut(
        code=code, expires_at=row.login_code_expires_at, session_id=row.id
    )


@router.post("/session", response_model=ConsoleSessionOut)
async def exchange_login_code(
    data: ConsoleSessionExchange,
    session: SessionDep,
    response: Response,
    origin: Annotated[str | None, Header()] = None,
) -> Any:
    """Exchange a login code for the session cookie. No auth dependency: the
    code is the credential, and the cookie is what this call establishes.

    The Origin gate runs BEFORE the code is touched, so a refusal costs the
    operator nothing: the code is still live to retry over the port-forward the
    fix names.
    """

    if not _is_secure_context(origin):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": (
                    "refusing to establish a console session from a non-secure "
                    f"origin {origin!r}: the session cookie is Secure, and a "
                    "browser would silently drop it over plaintext"
                ),
                "fix": _INSECURE_ORIGIN_FIX,
            },
        )

    settings = get_settings()
    exchanged = await crud.exchange_console_login_code(
        session, data.code, settings.console_session_ttl_seconds
    )
    if exchanged is None:
        # Unknown, spent, expired, or revoked are one refusal: which one it was
        # is not the caller's to learn.
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "invalid or expired login code"},
        )
    token, expires_at = exchanged
    response.set_cookie(
        CONSOLE_SESSION_COOKIE,
        token,
        max_age=settings.console_session_ttl_seconds,
        httponly=True,  # page script cannot read the credential it authenticates with
        secure=True,
        samesite="strict",  # the CSRF control (ADR-0049); the API runs no CORS
        path="/",
    )
    return ConsoleSessionOut(expires_at=expires_at)


@router.get("/session", response_model=ConsoleSessionStatus)
async def get_session_status(
    session: SessionDep,
    agentos_console_session: Annotated[str | None, Cookie()] = None,
) -> ConsoleSessionStatus:
    """Whether the caller's cookie names a live session, for the console's own
    "am I logged in" check. Anonymous is a 200 with authenticated=false, not a
    401: not being logged in is the answer, not an error."""

    row = (
        await crud.live_console_session(session, agentos_console_session)
        if agentos_console_session
        else None
    )
    if row is None:
        return ConsoleSessionStatus(authenticated=False, expires_at=None)
    return ConsoleSessionStatus(authenticated=True, expires_at=row.session_expires_at)


@router.delete("/session", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    session: SessionDep,
    agentos_console_session: Annotated[str | None, Cookie()] = None,
) -> Response:
    """Self-logout: revoke the session server-side and clear the cookie.

    Both halves matter. Clearing the cookie alone would leave a stolen copy of
    the token live, so the row write is the real revocation. Idempotent, so a
    logout without a live session is still a 204.
    """

    if agentos_console_session:
        await crud.revoke_console_session(session, agentos_console_session)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    # The attributes must match the ones the cookie was set with, or the browser
    # keeps its copy alongside the expired one.
    response.delete_cookie(
        CONSOLE_SESSION_COOKIE, path="/", httponly=True, secure=True, samesite="strict"
    )
    return response


@router.get(
    "/sessions",
    response_model=list[ConsoleSessionListItem],
    dependencies=[Depends(require_platform_key)],
)
async def list_sessions(session: SessionDep) -> list[ConsoleSessionListItem]:
    """The operator's inventory of console sessions.

    An inventory, never a way to read one: no digest and no raw credential is
    projected here, so a listing cannot be replayed into a login.
    """

    return [
        ConsoleSessionListItem(
            id=row.id,
            label=row.label,
            created_at=row.created_at,
            # The expiry that currently governs the row: the session's once the
            # code is exchanged, the login code's until then.
            expires_at=row.session_expires_at or row.login_code_expires_at,
            consumed_at=row.consumed_at,
            revoked_at=row.revoked_at,
        )
        for row in await crud.list_console_sessions(session)
    ]


@router.delete(
    "/sessions",
    response_model=ConsoleRevokeOut,
    dependencies=[Depends(require_platform_key)],
)
async def revoke_sessions(session: SessionDep) -> ConsoleRevokeOut:
    """Revoke every live console grant. The operator's kill switch for the
    console, and why the session is a durable row rather than a signed token:
    this takes effect on the next request, with no key rotation and no restart.
    """

    return ConsoleRevokeOut(revoked=await crud.revoke_all_console_sessions(session))
