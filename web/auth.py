"""
web/auth.py — authentication against WEB_ADMIN_PASSWORD.

Two accepted credentials:
  1. `Authorization: Bearer <password>` header — used by fetch() calls.
  2. `ce_session` cookie carrying a derived session token — needed because
     <video>/<img> tags cannot send headers, so clip/thumb media requests
     authenticate via the cookie the PWA sets after unlock
     (POST /api/auth/session).

The session token is HMAC-SHA256(key=password, msg="clip-engine-session-v1"),
so the cookie never contains the raw password and cannot be reversed to it.
All comparisons use hmac.compare_digest to prevent timing attacks.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

try:
    from core.settings import get_settings
    _WEB_ADMIN_PASSWORD: str = get_settings().web_admin_password or ""
except Exception:
    _WEB_ADMIN_PASSWORD = ""

logger = logging.getLogger(__name__)

_bearer_scheme = HTTPBearer(auto_error=False)


SESSION_COOKIE = "ce_session"
_SESSION_MSG = b"clip-engine-session-v1"


def _get_expected_password() -> str:
    """Return the expected password, preferring core.settings over os.environ."""
    if _WEB_ADMIN_PASSWORD:
        return _WEB_ADMIN_PASSWORD
    return os.environ.get("WEB_ADMIN_PASSWORD", "")


def session_token() -> str:
    """Derived session-cookie value (never the raw password)."""
    expected = _get_expected_password()
    return hmac.new(expected.encode("utf-8"), _SESSION_MSG, hashlib.sha256).hexdigest()


def require_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    """FastAPI dependency: accept a valid Bearer header OR session cookie.

    Raises HTTP 401 if:
    - Neither a Bearer header nor a ce_session cookie is presented.
    - The presented credential doesn't match (constant-time compare).
    - WEB_ADMIN_PASSWORD is not set (misconfiguration).

    Returns None on success (callers just declare Depends(require_auth)).
    """
    expected = _get_expected_password()
    if not expected:
        logger.error(
            "WEB_ADMIN_PASSWORD is not configured — all API requests will be rejected"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Server misconfiguration: WEB_ADMIN_PASSWORD not set",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Path 1: Bearer header (fetch() calls).
    if credentials is not None and credentials.scheme.lower() == "bearer":
        token_bytes = credentials.credentials.encode("utf-8")
        if hmac.compare_digest(token_bytes, expected.encode("utf-8")):
            return
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Path 2: session cookie (<video>/<img> media requests).
    cookie_val = request.cookies.get(SESSION_COOKIE, "")
    if cookie_val and hmac.compare_digest(
        cookie_val.encode("utf-8"), session_token().encode("utf-8")
    ):
        return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid credentials (Bearer header or session cookie)",
        headers={"WWW-Authenticate": "Bearer"},
    )
