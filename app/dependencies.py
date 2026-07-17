from __future__ import annotations

import logging
from typing import Any, Dict, Generator
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis import Redis
from sqlmodel import Session, select

from app.core.redis import get_redis_client
from app.core.security import decode_admin_jwt, decode_supabase_jwt, extract_role
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


async def get_admin_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Validate a custom admin JWT (issued by POST /api/admin/auth/step2).
    Checks both the JWT signature/expiry AND the JTI presence in Redis
    so that logout immediately revokes the session.

    For admin_accounts-backed sessions (invited admins, not the env-var
    bootstrap super admin), also re-checks the account's live status on
    every request — a suspension/deletion must take effect immediately,
    not just block the next login, even though the JWT itself might still
    be within its (short) validity window.
    """
    from app.config import get_settings as _get_settings

    token = credentials.credentials
    claims = decode_admin_jwt(token)
    if not claims:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    jti = claims.get("jti", "")
    if not jti or not get_redis_client().get(f"admin:session:{jti}"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin session expired or revoked",
            headers={"WWW-Authenticate": "Bearer"},
        )

    admin_id = claims.get("admin_id")
    if admin_id:
        from app.models.admin_account import AdminAccount

        account = db.get(AdminAccount, UUID(admin_id))
        if account is None or account.status != "active":
            get_redis_client().delete(f"admin:session:{jti}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Admin account suspended or removed",
                headers={"WWW-Authenticate": "Bearer"},
            )

    cfg = _get_settings()
    return {
        # "id" is ALWAYS None — deliberately unchanged from before multi-admin
        # support existed. Admins (bootstrap or invited via admin_accounts)
        # are never rows in `profiles`, so this must never be written into a
        # `foreign_key="profiles.id")` column (moderated_by, reviewed_by,
        # deleted_by, etc. across the codebase all guard on
        # `current_user.get("id")` and rely on it being falsy for admins).
        "id": None,
        # Real admin_accounts.id (None for the env-var bootstrap admin) —
        # only for code that specifically tracks admin_accounts actions
        # (see app/routers/admin/admin_accounts.py, AdminAccountAuditLog).
        # Never assign this into a profiles.id foreign key.
        "admin_account_id": claims.get("admin_id"),
        "email": claims.get("email") or cfg.admin_email,
        "role": claims.get("role") or "admin",
        "claims": claims,
    }


def require_super_admin(
    current_user: Dict[str, Any] = Depends(get_admin_user),
) -> Dict[str, Any]:
    """Dependency for endpoints restricted to the super admin (PDG) — either
    the original env-var bootstrap admin or an admin_accounts row with
    role='super_admin'."""
    if current_user.get("role") != "super_admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Réservé au super administrateur",
        )
    return current_user


def require_role(*roles: str):
    """Dependency factory — raises 403 if the JWT role isn't in `roles`."""

    async def _checker(
        current_user: Dict[str, Any] = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> Dict[str, Any]:
        if current_user["role"] not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Required roles: {list(roles)}",
            )
        # A suspended teacher (manual admin action, or the automatic
        # no-response penalty — see app/workers/booking_tasks.py) loses all
        # teacher-only access, not just visibility in student search/booking.
        if current_user["role"] == "teacher":
            from app.models.profile import TeacherProfile

            tp = db.get(TeacherProfile, UUID(current_user["id"]))
            if tp is not None and tp.status == "suspended":
                detail = "Votre compte est suspendu."
                if tp.suspended_until:
                    detail += f" Réactivation prévue le {tp.suspended_until.strftime('%d/%m/%Y à %Hh%M')}."
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
        return current_user

    return _checker
