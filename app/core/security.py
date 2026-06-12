from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

from jose import JWTError, jwt

from app.config import get_settings

settings = get_settings()

_ALGORITHM = "HS256"


def _jwt_secret() -> str:
    """
    Priority: SUPABASE_JWT_SECRET (from Supabase → Settings → API → JWT Secret)
    Fallback:  SUPABASE_SERVICE_ROLE_KEY (works for same-project tokens in dev).
    Set SUPABASE_JWT_SECRET in production — the service role key is not the JWT signing secret.
    """
    return settings.supabase_jwt_secret or settings.supabase_service_role_key


def decode_supabase_jwt(token: str) -> Optional[Dict[str, Any]]:
    """
    Validate a Supabase-issued JWT and return its claims dict.
    Returns None on any validation failure (expired, invalid signature, etc.).
    """
    try:
        claims = jwt.decode(
            token,
            _jwt_secret(),
            algorithms=[_ALGORITHM],
            options={"verify_aud": False},
        )
        return claims
    except JWTError:
        return None


def extract_user_id(claims: Dict[str, Any]) -> Optional[str]:
    """Return the Supabase auth UUID (= profiles.id)."""
    return claims.get("sub")


def _admin_jwt_secret() -> str:
    """Dedicated signing secret for admin JWTs (falls back to secret_key)."""
    return settings.admin_jwt_secret or settings.secret_key


def create_admin_jwt(jti: str) -> str:
    """Issue a signed admin session JWT containing the session JTI."""
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=settings.admin_jwt_expire_minutes)
    payload: Dict[str, Any] = {
        "sub": "admin",
        "role": "admin",
        "type": "admin_session",
        "jti": jti,
        "iat": now,
        "exp": expire,
    }
    return jwt.encode(payload, _admin_jwt_secret(), algorithm=_ALGORITHM)


def decode_admin_jwt(token: str) -> Optional[Dict[str, Any]]:
    """Validate a custom admin JWT. Returns None on any failure."""
    try:
        claims = jwt.decode(token, _admin_jwt_secret(), algorithms=[_ALGORITHM])
        if claims.get("type") != "admin_session":
            return None
        return claims
    except JWTError:
        return None


def extract_role(claims: Dict[str, Any]) -> str:
    """
    Extract the app role from JWT claims.
    Supabase stores custom claims in app_metadata.
    Falls back to user_metadata.role, then 'student'.
    """
    role = (
        claims.get("app_metadata", {}).get("role")
        or claims.get("user_metadata", {}).get("role")
        or "student"
    )
    return role
