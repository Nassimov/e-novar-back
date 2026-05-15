from __future__ import annotations

import random
import string
from typing import Any, Dict, Optional
from uuid import UUID

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
) -> Profile:
    """
    Fetch the profile row for `supabase_id`.
    If the Supabase trigger didn't fire yet (edge case), insert a minimal profile.
    Always updates first_name / last_name / phone when provided.
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
    return "".join(random.choices(string.digits, k=length))


def send_otp_email(email: str) -> str:
    """Generate OTP, store in Redis with 10-min TTL, return the code."""
    code = generate_otp_code()
    redis = get_redis_client()
    redis.setex(f"otp:{email}", 600, code)
    return code


def verify_otp(email: str, code: str) -> bool:
    """Verify OTP and delete on success."""
    redis = get_redis_client()
    stored = redis.get(f"otp:{email}")
    if stored and stored == code:
        redis.delete(f"otp:{email}")
        return True
    return False


def register_user_in_supabase(
    email: str, password: str, role: str, full_name: str, phone: Optional[str] = None
) -> Any:
    """Register a new user via Supabase Auth sign_up (anon key — no admin key needed)."""
    from app.database import get_supabase_anon
    client = get_supabase_anon()
    parts = full_name.strip().split(" ", 1)
    user_metadata: Dict[str, Any] = {
        "role": role,
        "full_name": full_name,
        "first_name": parts[0],
        "last_name": parts[1] if len(parts) > 1 else "",
    }
    if phone:
        user_metadata["phone"] = phone
    return client.auth.sign_up({
        "email": email,
        "password": password,
        "options": {"data": user_metadata},
    })


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


def send_password_reset(email: str) -> None:
    from app.database import get_supabase_anon

    get_supabase_anon().auth.reset_password_email(
        email,
        options={"redirect_to": f"{settings.frontend_url}/reset-password"},
    )


def reset_password_with_token(token: str, new_password: str) -> None:
    from app.database import get_supabase_anon

    get_supabase_anon().auth.update_user({"password": new_password})


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
