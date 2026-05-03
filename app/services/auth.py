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
    db: Session = None,
) -> Profile:
    """
    Fetch the profile row for `supabase_id`.
    If the Supabase trigger didn't fire yet (edge case), insert a minimal profile.
    The handle_new_user() trigger should have already created the row and a
    kp_balances + notification_preferences row — we do NOT duplicate those here.
    """
    uid = UUID(supabase_id)
    profile = db.exec(select(Profile).where(Profile.id == uid)).first()

    if profile is None:
        # Trigger missed — create the minimal profile manually.
        profile = Profile(
            id=uid,
            email=email,
            first_name=first_name or email.split("@")[0],
            last_name=last_name,
        )
        db.add(profile)
        # Register default role
        existing_role = db.exec(
            select(UserRole).where(UserRole.user_id == uid, UserRole.role == role)
        ).first()
        if not existing_role:
            db.add(UserRole(user_id=uid, role=role))
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
    email: str, password: str, role: str, full_name: str
) -> Any:
    """Create a new user in Supabase Auth with role metadata."""
    client = get_supabase_service()
    # Split full_name into first/last for the profile trigger
    parts = full_name.strip().split(" ", 1)
    first_name = parts[0]
    last_name = parts[1] if len(parts) > 1 else ""
    return client.auth.admin.create_user(
        {
            "email": email,
            "password": password,
            "user_metadata": {
                "role": role,
                "full_name": full_name,
                "first_name": first_name,
                "last_name": last_name,
            },
            "app_metadata": {"role": role},
            "email_confirm": True,
        }
    )


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
