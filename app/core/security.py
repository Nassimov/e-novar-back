from __future__ import annotations

from typing import Any, Dict, Optional

from jose import JWTError, jwt

from app.config import get_settings

settings = get_settings()

SUPABASE_JWT_ALGORITHM = "HS256"


def decode_supabase_jwt(token: str) -> Optional[Dict[str, Any]]:
    """Decode and verify a Supabase JWT token.

    Returns the claims dict on success, or None if invalid.
    """
    try:
        # Supabase JWTs are signed with the project JWT secret (same as the anon key secret)
        # We use the supabase_anon_key as the secret for local verification
        # In production, use the JWT Secret from Supabase project settings
        secret = settings.supabase_service_role_key or settings.supabase_anon_key
        claims = jwt.decode(
            token,
            secret,
            algorithms=[SUPABASE_JWT_ALGORITHM],
            options={"verify_aud": False},
        )
        return claims
    except JWTError:
        return None


def extract_user_id(claims: Dict[str, Any]) -> Optional[str]:
    return claims.get("sub")


def extract_user_role(claims: Dict[str, Any]) -> str:
    app_role = claims.get("app_metadata", {}).get("role")
    if app_role:
        return app_role
    user_role = claims.get("user_metadata", {}).get("role")
    if user_role:
        return user_role
    return "student"


def create_internal_token(user_id: str, role: str) -> str:
    """Create a short-lived internal token for service-to-service calls."""
    from datetime import datetime, timedelta

    payload = {
        "sub": user_id,
        "role": role,
        "exp": datetime.utcnow() + timedelta(minutes=15),
        "iat": datetime.utcnow(),
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")
