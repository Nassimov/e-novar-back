from __future__ import annotations

import hashlib
import logging
import secrets
import string
from typing import Any, Dict, Optional
from uuid import UUID

logger = logging.getLogger(__name__)

from sqlmodel import Session, select

from app.config import get_settings
from app.core.redis import get_redis_client
from app.core.security import decode_supabase_jwt
from app.database import get_supabase_service
from app.models.profile import Profile, UserRole

settings = get_settings()


def verify_supabase_token(token: str) -> Optional[Dict[str, Any]]:
    return decode_supabase_jwt(token)


def get_or_create_profile(
    supabase_id: str,
    email: str,
    role: str,
    first_name: str = "",
    last_name: str = "",
    phone: Optional[str] = None,
    db: Session = None,
    email_verified: bool = False,
) -> Profile:
    """
    Fetch the profile row for `supabase_id`.
    If the Supabase trigger didn't fire yet (edge case), insert a minimal profile.
    Always updates first_name / last_name / phone when provided.

    email_verified: only applied to a NEWLY created profile — Google OAuth
    callers pass True (Google already verified the address, no need for our
    own email-link flow); the plain email/password register() flow leaves
    this False so the new verify-email gate applies.
    """
    uid = UUID(supabase_id)
    profile = db.exec(select(Profile).where(Profile.id == uid)).first()

    if profile is None:
        profile = Profile(
            id=uid,
            email=email,
            first_name=first_name or email.split("@")[0],
            last_name=last_name,
            phone=phone,
            email_verified=email_verified,
        )
        db.add(profile)
        existing_role = db.exec(
            select(UserRole).where(UserRole.user_id == uid, UserRole.role == role)
        ).first()
        if not existing_role:
            db.add(UserRole(user_id=uid, role=role))
        db.commit()
        db.refresh(profile)
    else:
        # Update fields that may not have been set by the trigger
        changed = False
        if first_name and not profile.first_name:
            profile.first_name = first_name
            changed = True
        if last_name and not profile.last_name:
            profile.last_name = last_name
            changed = True
        if phone and not profile.phone:
            profile.phone = phone
            changed = True
        if changed:
            db.add(profile)
            db.commit()
            db.refresh(profile)

    return profile


def ensure_role(supabase_id: str, role: str, db: Session) -> None:
    """Idempotently add a role entry to user_roles for the given user."""
    uid = UUID(supabase_id)
    existing = db.exec(
        select(UserRole).where(UserRole.user_id == uid, UserRole.role == role)
    ).first()
    if not existing:
        db.add(UserRole(user_id=uid, role=role))
        db.commit()


def generate_otp_code(length: int = 6) -> str:
    """Cryptographically random — this used to be `random.choices` (not
    suitable for anything security-sensitive)."""
    return "".join(secrets.choice(string.digits) for _ in range(length))


def _hash_otp(code: str) -> str:
    """Redis stores only this hash, never the plaintext code — a leaked
    Redis snapshot/log line shouldn't hand out a live reset code."""
    return hashlib.sha256(code.encode()).hexdigest()


def request_password_reset_code(email: str) -> None:
    """Best-effort: if a profile with this email exists, generate a 6-digit
    code, store its hash in Redis (10-min TTL), and email it. Silently
    no-ops for an unregistered email — app/routers/auth.py's forgot_password
    always returns the same generic message either way, so this never
    reveals whether an address is registered."""
    from sqlmodel import Session, select
    from app.database import get_engine

    with Session(get_engine()) as db:
        profile = db.exec(select(Profile).where(Profile.email == email)).first()
        if profile is None:
            return

    code = generate_otp_code()
    redis = get_redis_client()
    redis.setex(f"otp:{email}", 600, _hash_otp(code))

    from app.workers.email_tasks import send_password_reset_code_email
    send_password_reset_code_email.delay(email, code)


def verify_otp(email: str, code: str) -> Optional[str]:
    """Verify the emailed code (single-use — deleted from Redis on match).
    Returns a short-lived signed password_reset token on success (see
    app/core/security.py::create_password_reset_jwt), or None on any
    failure (wrong code, expired, or no profile for this email)."""
    redis = get_redis_client()
    stored_hash = redis.get(f"otp:{email}")
    if not stored_hash or stored_hash != _hash_otp(code):
        return None
    redis.delete(f"otp:{email}")

    from sqlmodel import Session, select
    from app.database import get_engine
    from app.core.security import create_password_reset_jwt

    with Session(get_engine()) as db:
        profile = db.exec(select(Profile).where(Profile.email == email)).first()
        if profile is None:
            return None
        return create_password_reset_jwt(str(profile.id))


def reset_password_with_token(token: str, new_password: str) -> None:
    """Set a new password via the Supabase admin/service-role client — the
    only client that can set an arbitrary user's password without an
    active session for them (the previous implementation called
    update_user() on a shared anon client with no session ever set on it,
    so it never actually worked)."""
    from app.core.security import decode_password_reset_jwt

    user_id = decode_password_reset_jwt(token)
    if user_id is None:
        raise Exception("Invalid or expired reset token")
    get_supabase_service().auth.admin.update_user_by_id(user_id, {"password": new_password})


def register_user_in_supabase(
    email: str, password: str, role: str, full_name: str, phone: Optional[str] = None
) -> Any:
    """Register a new user and always return a valid session.

    Uses admin.create_user() with email_confirm=True so Supabase never sends
    a confirmation email (avoids the email rate limit on free plan).
    The handle_new_user DB trigger still fires on auth.users INSERT.
    """
    from app.database import get_supabase_anon
    parts = full_name.strip().split(" ", 1)
    user_metadata: Dict[str, Any] = {
        "role": role,
        "full_name": full_name,
        "first_name": parts[0],
        "last_name": parts[1] if len(parts) > 1 else "",
    }
    if phone:
        user_metadata["phone"] = phone

    admin_client = get_supabase_service()
    result = admin_client.auth.admin.create_user({
        "email": email,
        "password": password,
        "email_confirm": True,
        "user_metadata": user_metadata,
        "app_metadata": {"role": role},
    })

    if not result.user:
        raise Exception("User creation failed")

    # Sign in with the anon client to get a valid session
    anon_client = get_supabase_anon()
    login_result = anon_client.auth.sign_in_with_password({"email": email, "password": password})
    return login_result


def login_with_supabase(email: str, password: str) -> Any:
    from app.database import get_supabase_anon

    client = get_supabase_anon()
    return client.auth.sign_in_with_password({"email": email, "password": password})


def logout_from_supabase(access_token: str) -> None:
    from app.database import get_supabase_anon

    get_supabase_anon().auth.sign_out()


def refresh_supabase_token(refresh_token: str) -> Any:
    from app.database import get_supabase_anon

    return get_supabase_anon().auth.refresh_session(refresh_token)


def exchange_google_code(code: str, code_verifier: Optional[str] = None) -> Any:
    """
    Exchange a Supabase OAuth code for a session (PKCE flow).
    `code_verifier` is required when the frontend initiated OAuth with PKCE
    (Supabase default). Omit only for implicit/non-PKCE flows.
    """
    from app.database import get_supabase_anon

    params: Dict[str, Any] = {"auth_code": code}
    if code_verifier:
        params["code_verifier"] = code_verifier
    return get_supabase_anon().auth.exchange_code_for_session(params)


def get_supabase_user_from_token(access_token: str) -> Any:
    """Validate a Supabase JWT and return the matching auth.users record."""
    from app.database import get_supabase_service

    return get_supabase_service().auth.get_user(access_token)
