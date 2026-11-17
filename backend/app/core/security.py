"""Demo authentication.

DEMONSTRATION ONLY: the user is identified by the X-User-Id header and administrators by
X-Admin-Token. Production would use OAuth2/OIDC with signed JWTs, server-side sessions,
MFA for admins, and per-role authorisation.
"""
from __future__ import annotations

import re
import secrets

from fastapi import Header

from app.core.config import get_settings
from app.core.errors import ApiError

_USER_RE = re.compile(r"^[A-Za-z0-9_\-]{3,64}$")


def validate_user_id(user_id: str) -> str:
    if not _USER_RE.match(user_id or ""):
        raise ApiError(400, "INVALID_USER", "X-User-Id must be 3-64 chars of letters, digits, '-' or '_'")
    return user_id


async def current_user(x_user_id: str = Header(..., alias="X-User-Id")) -> str:
    return validate_user_id(x_user_id)


def is_admin(token: str | None) -> bool:
    expected = get_settings().admin_token
    return bool(token) and secrets.compare_digest(token.encode(), expected.encode())


async def require_admin(x_admin_token: str | None = Header(None, alias="X-Admin-Token")) -> None:
    if not is_admin(x_admin_token):
        raise ApiError(401, "ADMIN_AUTH_REQUIRED", "Valid X-Admin-Token header required")


async def require_dev_admin(x_admin_token: str | None = Header(None, alias="X-Admin-Token")) -> None:
    if not get_settings().dev_endpoints_allowed:
        raise ApiError(403, "DEV_ENDPOINTS_DISABLED", "Destructive/dev endpoints are disabled in this environment")
    await require_admin(x_admin_token)
