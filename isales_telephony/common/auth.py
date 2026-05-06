"""FastAPI JWT verification dependency.

Wraps :func:`isales_common.utils.jwt.verify_jwt` and reads the secret from
``ISALES_JWT_SECRET``. The architecture spec says telephony-api MAY verify but
MUST NOT sign — this module only verifies. Internal-only routes (e.g.
``/devices/select`` called by scheduler over loopback) MAY skip this dependency.
"""

from __future__ import annotations

import os
from typing import Annotated, Any

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from isales_common.utils.jwt import InvalidJWT, verify_jwt

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=True)


def _secret() -> str:
    secret = os.environ.get("ISALES_JWT_SECRET")
    if not secret:
        raise RuntimeError("ISALES_JWT_SECRET is not set")
    return secret


def current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
) -> dict[str, Any]:
    """Resolve the JWT to its claim dict; 401 on invalid / expired tokens."""
    try:
        return verify_jwt(token, _secret())
    except InvalidJWT as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


CurrentUser = Annotated[dict[str, Any], Depends(current_user)]
