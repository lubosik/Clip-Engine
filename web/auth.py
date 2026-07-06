"""
web/auth.py — HTTP bearer token authentication against WEB_ADMIN_PASSWORD.

Usage (FastAPI dependency):

    from web.auth import require_auth

    @app.get("/api/clips")
    def get_clips(auth: None = Depends(require_auth)):
        ...

The password is compared using hmac.compare_digest to prevent timing attacks.
"""

from __future__ import annotations

import hmac
import logging
import os

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

try:
    from core.settings import settings as _settings
    _WEB_ADMIN_PASSWORD: str = _settings.web_admin_password or ""
except Exception:
    _WEB_ADMIN_PASSWORD = ""

logger = logging.getLogger(__name__)

_bearer_scheme = HTTPBearer(auto_error=False)


def _get_expected_password() -> str:
    """Return the expected password, preferring core.settings over os.environ."""
    if _WEB_ADMIN_PASSWORD:
        return _WEB_ADMIN_PASSWORD
    return os.environ.get("WEB_ADMIN_PASSWORD", "")


def require_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    """FastAPI dependency that enforces bearer token auth.

    Raises HTTP 401 if:
    - The Authorization header is absent.
    - The scheme is not Bearer.
    - The token does not match WEB_ADMIN_PASSWORD (constant-time compare).
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

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header (expected: Bearer <token>)",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Constant-time comparison to prevent timing attacks.
    token_bytes = credentials.credentials.encode("utf-8")
    expected_bytes = expected.encode("utf-8")
    if not hmac.compare_digest(token_bytes, expected_bytes):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
