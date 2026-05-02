from __future__ import annotations

import random
import string
from typing import Any, Dict, Optional

from sqlmodel import Session, select

from app.config import get_settings
from app.core.redis import get_redis_client
from app.core.security import decode_supabase_jwt
from app.database import get_supabase_service
from app.models.user import User, UserRole

settings = get_settings()


def verify_supabase_token(token: str) -> Optional[Dict[str, Any]]:
    """Decode and verify a Supabase JWT. Returns claims dict or None."""
    return decode_supabase_jwt(token)


def get_or_create_user(
    supabase_id: str,
    email: str,
    role: str,
    full_name: str,
    db: Session,
) -> User:
    """Upsert a user in the local database based on their Supabase ID."""
    statement = select(User).where(User.supabase_id == supabase_id)
    user = db.exec(statement).first()

    if user is None:
        user = User(
            supabase_id=supabase_id,
            email=email,
            role=UserRole(role) if role in UserRole.__members__ else UserRole.student,
            full_name=full_name or email.split("@")[0],
        )
        db.add(user)
        db.commit()
        db.refresh(user)

    return user


def generate_otp_code(length: int = 6) -> str:
    return "".join(random.choices(string.digits, k=length))


def send_otp_email(email: str) -> str:
    """Generate OTP, store in Redis with 10min TTL, return the code."""
    code = generate_otp_code()
    redis = get_redis_client()
    redis.setex(f"otp:{email}", 600, code)  # 10 minutes
    # In production, dispatch email_tasks.send_otp_email.delay(email, code)
    return code


def verify_otp(email: str, code: str) -> bool:
    """Verify an OTP code for the given email. Deletes the code on success."""
    redis = get_redis_client()
    stored = redis.get(f"otp:{email}")
    if stored and stored == code:
        redis.delete(f"otp:{email}")
        return True
    return False


def register_user_in_supabase(email: str, password: str, role: str, full_name: str) -> Dict[str, Any]:
    """Create a new user in Supabase Auth with metadata."""
    client = get_supabase_service()
    result = client.auth.admin.create_user(
        {
            "email": email,
            "password": password,
            "user_metadata": {"role": role, "full_name": full_name},
            "email_confirm": True,
        }
    )
    return result


def login_with_supabase(email: str, password: str) -> Dict[str, Any]:
    """Sign in via Supabase Auth (anon key flow)."""
    from app.database import get_supabase_anon

    client = get_supabase_anon()
    result = client.auth.sign_in_with_password({"email": email, "password": password})
    return result


def logout_from_supabase(access_token: str) -> None:
    """Sign out and invalidate the user's session."""
    from app.database import get_supabase_anon

    client = get_supabase_anon()
    # Set the session so we can sign out
    client.auth.sign_out()


def refresh_supabase_token(refresh_token: str) -> Dict[str, Any]:
    """Use a refresh token to get a new access token."""
    from app.database import get_supabase_anon

    client = get_supabase_anon()
    result = client.auth.refresh_session(refresh_token)
    return result


def send_password_reset(email: str) -> None:
    """Send password reset email via Supabase."""
    from app.database import get_supabase_anon

    client = get_supabase_anon()
    client.auth.reset_password_email(
        email,
        options={"redirect_to": f"{settings.frontend_url}/reset-password"},
    )


def reset_password_with_token(token: str, new_password: str) -> None:
    """Reset password using the token from email."""
    from app.database import get_supabase_anon

    client = get_supabase_anon()
    client.auth.update_user({"password": new_password})
