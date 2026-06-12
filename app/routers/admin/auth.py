from __future__ import annotations

import secrets
import uuid
from typing import Any, Dict

import bcrypt as _bcrypt

from fastapi import APIRouter, Body, HTTPException, Request, status
from pydantic import BaseModel

from app.config import get_settings
from app.core.redis import get_redis_client
from app.core.security import create_admin_jwt, decode_admin_jwt

router = APIRouter(tags=["Admin — Auth"])
settings = get_settings()


def _verify_password(plain: str, hashed: str) -> bool:
    """bcrypt verify — avoids passlib/bcrypt 4.x compatibility bug."""
    try:
        return _bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False

_MAX_ATTEMPTS = 5
_LOCKOUT_TTL = 900       # 15 min lockout after max failures
_CHALLENGE_TTL = 300     # 5 min to enter TOTP after step-1

# ── helpers ───────────────────────────────────────────────────────────────────

def _ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return (request.client.host if request.client else "") or "unknown"


def _fail_key(ip: str) -> str:
    return f"admin:login_failures:{ip}"


def _guard_brute_force(ip: str) -> None:
    redis = get_redis_client()
    attempts = redis.get(_fail_key(ip))
    if attempts and int(attempts) >= _MAX_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed attempts. Try again in 15 minutes.",
        )


def _record_failure(ip: str) -> None:
    redis = get_redis_client()
    key = _fail_key(ip)
    pipe = redis.pipeline()
    pipe.incr(key)
    pipe.expire(key, _LOCKOUT_TTL)
    pipe.execute()


def _clear_failures(ip: str) -> None:
    get_redis_client().delete(_fail_key(ip))


# ── schemas ───────────────────────────────────────────────────────────────────

class Step1Request(BaseModel):
    email: str
    password: str


class Step2Request(BaseModel):
    challenge_token: str
    totp_code: str


# ── endpoints ─────────────────────────────────────────────────────────────────

@router.post("/step1", summary="Admin login — step 1: email + password")
async def admin_step1(payload: Step1Request, request: Request) -> Dict[str, Any]:
    """
    Validate admin email and bcrypt password.
    On success, returns a short-lived `challenge_token` required for step 2.
    The challenge token expires in 5 minutes and is stored exclusively in Redis.
    """
    ip = _ip(request)
    _guard_brute_force(ip)

    # Both checks run to prevent enumeration (don't short-circuit on email mismatch)
    email_ok = payload.email.strip().lower() == settings.admin_email.strip().lower()
    hash_ok = bool(settings.admin_password_hash) and _verify_password(
        payload.password, settings.admin_password_hash
    )

    if not email_ok or not hash_ok:
        _record_failure(ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    _clear_failures(ip)

    challenge = secrets.token_urlsafe(48)
    get_redis_client().setex(f"admin:challenge:{challenge}", _CHALLENGE_TTL, "1")

    return {"challenge_token": challenge}


@router.post("/step2", summary="Admin login — step 2: TOTP verification")
async def admin_step2(payload: Step2Request, request: Request) -> Dict[str, Any]:
    """
    Validate the TOTP code and the challenge token from step 1.
    On success, returns a signed admin JWT.  The challenge token is consumed
    (one-time use) to prevent replay attacks.
    """
    import pyotp

    ip = _ip(request)
    _guard_brute_force(ip)

    redis = get_redis_client()
    challenge_key = f"admin:challenge:{payload.challenge_token}"

    # Validate challenge token
    if not redis.get(challenge_key):
        _record_failure(ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired challenge token",
        )

    # Validate TOTP
    if not settings.admin_2fa_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="2FA is not configured on this server",
        )

    totp = pyotp.TOTP(settings.admin_2fa_secret)
    if not totp.verify(payload.totp_code.strip(), valid_window=1):
        _record_failure(ip)
        # Do NOT delete the challenge on TOTP failure so admin can retry once
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid 2FA code",
        )

    # Consume challenge (one-time use)
    redis.delete(challenge_key)
    _clear_failures(ip)

    # Issue admin session JWT
    jti = str(uuid.uuid4())
    expire_seconds = settings.admin_jwt_expire_minutes * 60
    redis.setex(f"admin:session:{jti}", expire_seconds, "1")

    token = create_admin_jwt(jti)
    return {
        "access_token": token,
        "token_type": "bearer",
        "email": settings.admin_email,
        "expires_in": expire_seconds,
    }


@router.post("/logout", summary="Admin logout — invalidate session")
async def admin_logout(request: Request) -> Dict[str, str]:
    """
    Invalidate the admin session by removing the JTI from Redis.
    Accepts the token from the Authorization header.
    After this call the JWT is permanently revoked even if not yet expired.
    """
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        claims = decode_admin_jwt(token)
        if claims and claims.get("jti"):
            get_redis_client().delete(f"admin:session:{claims['jti']}")

    return {"message": "Admin logged out successfully"}
