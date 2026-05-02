from __future__ import annotations

from typing import Any, Dict, Generator

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis import Redis
from sqlmodel import Session

from app.core.redis import get_redis_client
from app.core.security import decode_supabase_jwt
from app.database import get_session

security = HTTPBearer()


def get_db() -> Generator[Session, None, None]:
    yield from get_session()


def get_redis() -> Redis:
    return get_redis_client()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> Dict[str, Any]:
    token = credentials.credentials
    claims = decode_supabase_jwt(token)
    if not claims:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user_id = claims.get("sub")
    email = claims.get("email", "")
    role = claims.get("user_metadata", {}).get("role", "student")
    app_metadata_role = claims.get("app_metadata", {}).get("role")
    if app_metadata_role:
        role = app_metadata_role
    return {"id": user_id, "email": email, "role": role, "claims": claims}


def require_role(*roles: str):
    async def _checker(current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
        if current_user["role"] not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Required roles: {list(roles)}",
            )
        return current_user

    return _checker
