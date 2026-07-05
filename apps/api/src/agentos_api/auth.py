"""Single-API-key authentication.

MVP auth is one shared key delivered in the `X-API-Key` header and compared
against Settings.api_key. J1 replaces this with GitHub-App-scoped identities.
"""

import hmac
from typing import Annotated

from fastapi import Header, HTTPException, status

from .config import get_settings

API_KEY_HEADER = "X-API-Key"


async def require_api_key(
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    expected = get_settings().api_key
    if x_api_key is None or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid API key",
        )
