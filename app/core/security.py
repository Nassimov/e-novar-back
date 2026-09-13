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


def create_admin_jwt(
    jti: str,
    admin_id: Optional[str] = None,
    email: Optional[str] = None,
    role: str = "admin",
) -> str:
    """Issue a signed admin session JWT containing the session JTI.

    `admin_id=None` identifies the original env-var bootstrap super-admin
    (see app/routers/admin/auth.py) — it has no row in admin_accounts.
    Any other admin passes their real `admin_accounts.id` here (see
    app/services/admin_accounts.py), so get_admin_user can tell admins
    apart and enforce super_admin-only actions.
    """
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=settings.admin_jwt_expire_minutes)
    payload: Dict[str, Any] = {
        "sub": admin_id or "admin",
        "admin_id": admin_id,
        "email": email,
        "role": role,
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


def _camera_jwt_secret() -> str:
    """Dedicated signing secret for camera-pairing JWTs (falls back to secret_key)."""
    return settings.camera_jwt_secret or settings.secret_key


def create_camera_jwt(
    camera_id: str,
    session_id: str,
    room_key: str,
    expire_at: datetime,
) -> str:
    """Issue a signed, session-scoped JWT for a paired second-camera device.

    Deliberately minimal claims — no profile/user identity of any kind, and
    `type` is checked by get_current_camera so this can never be accepted
    where a Supabase or admin JWT is expected (or vice versa). `expire_at`
    is the session's own grace end (see app/routers/classroom.py's
    scheduled_end + 45min), not a fixed TTL — a paired phone should never
    need to re-pair mid-lesson just because a generic timer ran out.
    """
    now = datetime.now(timezone.utc)
    payload: Dict[str, Any] = {
        "sub": f"camera:{camera_id}",
        "camera_id": camera_id,
        "session_id": session_id,
        "room_key": room_key,
        "type": "camera_session",
        "iat": now,
        "exp": expire_at,
    }
    return jwt.encode(payload, _camera_jwt_secret(), algorithm=_ALGORITHM)


def decode_camera_jwt(token: str) -> Optional[Dict[str, Any]]:
    """Validate a camera-pairing JWT. Returns None on any failure."""
    try:
        claims = jwt.decode(token, _camera_jwt_secret(), algorithms=[_ALGORITHM])
        if claims.get("type") != "camera_session":
            return None
        return claims
    except JWTError:
        return None


def create_email_verification_jwt(user_id: str) -> str:
    """Issue a registration email-verification link token (see
    app/routers/auth.py's POST /verify-email). 48h expiry — long enough
    that a slow inbox check doesn't lock someone out of an account they
    just created, short enough to bound how long a leaked/old link stays
    exploitable. The only action a valid token can trigger is flipping
    that one user's own Profile.email_verified to True — nothing else."""
    now = datetime.now(timezone.utc)
    payload: Dict[str, Any] = {
        "sub": user_id,
        "type": "email_verification",
        "iat": now,
        "exp": now + timedelta(hours=48),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=_ALGORITHM)


def decode_email_verification_jwt(token: str) -> Optional[str]:
    """Validate an email-verification token, returning the user_id or None."""
    try:
        claims = jwt.decode(token, settings.secret_key, algorithms=[_ALGORITHM])
        if claims.get("type") != "email_verification":
            return None
        return claims.get("sub")
    except JWTError:
        return None


def create_password_reset_jwt(user_id: str) -> str:
    """Issue a short-lived password-reset token, minted only after the
    user has already proven control of their inbox by typing the emailed
    OTP code correctly (see app/routers/auth.py's POST /verify-otp and
    POST /reset-password). 15 minutes — this is the LAST step of an
    already-completed verification, not a standalone link, so it can be
    short-lived without hurting UX."""
    now = datetime.now(timezone.utc)
    payload: Dict[str, Any] = {
        "sub": user_id,
        "type": "password_reset",
        "iat": now,
        "exp": now + timedelta(minutes=15),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=_ALGORITHM)


def decode_password_reset_jwt(token: str) -> Optional[str]:
    """Validate a password-reset token, returning the user_id or None."""
    try:
        claims = jwt.decode(token, settings.secret_key, algorithms=[_ALGORITHM])
        if claims.get("type") != "password_reset":
            return None
        return claims.get("sub")
    except JWTError:
        return None


def create_unsubscribe_jwt(user_id: str) -> str:
    """Issue a one-click marketing-email unsubscribe link token (see
    app/routers/notifications.py's GET /unsubscribe). Deliberately no `exp`
    — a campaign email can sit unread in an inbox for months, and the link
    inside it must still work whenever it's eventually clicked. Low-stakes
    by design: the only action a valid token can ever trigger is flipping
    one user's own marketing/email preference off, nothing else, so reusing
    `settings.secret_key` (no dedicated secret) is fine here."""
    payload: Dict[str, Any] = {
        "sub": user_id,
        "type": "unsubscribe_marketing",
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=_ALGORITHM)


def decode_unsubscribe_jwt(token: str) -> Optional[str]:
    """Validate an unsubscribe token, returning the user_id or None."""
    try:
        claims = jwt.decode(token, settings.secret_key, algorithms=[_ALGORITHM])
        if claims.get("type") != "unsubscribe_marketing":
            return None
        return claims.get("sub")
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
