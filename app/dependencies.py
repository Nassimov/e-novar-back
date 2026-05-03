from __future__ import annotations

from typing import Any, Dict, Generator
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis import Redis
from sqlmodel import Session, select

from app.core.redis import get_redis_client
from app.core.security import decode_supabase_jwt, extract_role, extract_user_id
from app.database import get_session
from app.models.profile import Profile

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
    """
    token = credentials.credentials
    claims = decode_supabase_jwt(token)
    if not claims:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return {
        "id": claims.get("sub", ""),
        "email": claims.get("email", ""),
        "role": extract_role(claims),
        "claims": claims,
    }


def get_current_profile(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Profile:
    """
    Resolve the JWT subject to a Profile row.
    Raises 404 if the profile doesn't exist yet
    (Supabase trigger may not have fired or user hasn't completed onboarding).
    """
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
