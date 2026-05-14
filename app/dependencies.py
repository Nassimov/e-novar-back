from __future__ import annotations

import logging
from typing import Any, Dict, Generator
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis import Redis
from sqlmodel import Session, select

from app.core.redis import get_redis_client
from app.core.security import decode_supabase_jwt, extract_role
from app.database import get_session
from app.models.profile import Profile

logger = logging.getLogger(__name__)
security = HTTPBearer()


def get_db() -> Generator[Session, None, None]:
    yield from get_session()


def get_redis() -> Redis:
    return get_redis_client()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> Dict[str, Any]:
    """
    Validate the Supabase JWT and return a dict with:
      id    : str  — Supabase auth UUID (= profiles.id)
      email : str
      role  : str  — from app_metadata.role or user_metadata.role
      claims: dict — full JWT payload

    Strategy:
      1. Try fast local HMAC verification (requires SUPABASE_JWT_SECRET in env).
      2. If that fails (secret misconfigured / missing), fall back to Supabase
         Auth API get_user() — always correct, one network round-trip.
    """
    token = credentials.credentials

    # ── Fast path: local JWT verification ────────────────────────────────────
    claims = decode_supabase_jwt(token)
    if claims:
        return {
            "id": claims.get("sub", ""),
            "email": claims.get("email", ""),
            "role": extract_role(claims),
            "claims": claims,
        }

    # ── Fallback: Supabase Auth API (works even without SUPABASE_JWT_SECRET) ─
    # This is the safety net when the JWT secret is not configured in env vars.
    logger.warning(
        "Local JWT decode failed — falling back to Supabase get_user() API. "
        "Set SUPABASE_JWT_SECRET in Railway/env to avoid this extra network call."
    )
    try:
        from app.database import get_supabase_service
        resp = get_supabase_service().auth.get_user(token)
        if resp and resp.user:
            su = resp.user
            role = (
                (su.app_metadata or {}).get("role")
                or (su.user_metadata or {}).get("role")
                or "student"
            )
            return {
                "id": str(su.id),
                "email": su.email or "",
                "role": role,
                "claims": {
                    "sub": str(su.id),
                    "email": su.email,
                    "app_metadata": su.app_metadata or {},
                    "user_metadata": su.user_metadata or {},
                },
            }
    except Exception as exc:
        logger.error("Supabase get_user fallback failed: %s", exc)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_profile(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Profile:
    user_id = UUID(current_user["id"])
    profile = db.exec(select(Profile).where(Profile.id == user_id)).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile


def require_role(*roles: str):
    """Dependency factory — raises 403 if the JWT role isn't in `roles`."""

    async def _checker(
        current_user: Dict[str, Any] = Depends(get_current_user),
    ) -> Dict[str, Any]:
        if current_user["role"] not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Required roles: {list(roles)}",
            )
        return current_user

    return _checker
