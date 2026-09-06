from __future__ import annotations

import json
import secrets
import uuid
from typing import Any, Dict, Optional

import bcrypt as _bcrypt

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlmodel import Session, select

from app.config import get_settings
from app.core.rate_limit import check_rate_limit, get_client_ip
from app.core.redis import get_redis_client
from app.core.security import create_admin_jwt, decode_admin_jwt
from app.dependencies import get_db
from app.models.admin_account import AdminAccount

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
# get_client_ip (X-Forwarded-For aware) lives in app.core.rate_limit — shared
# with app/routers/auth.py so both files agree on how the client IP is derived.

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
def admin_step1(
    payload: Step1Request,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Validate admin email + password — either the original env-var bootstrap
    super admin, or an active row in admin_accounts (invited by another
    admin, see app/services/admin_accounts.py).
    On success, returns a short-lived `challenge_token` required for step 2.
    The challenge token expires in 5 minutes and is stored exclusively in Redis,
    tagged with which identity it belongs to so step 2 checks the right TOTP secret.
    """
    ip = get_client_ip(request)
    email = payload.email.strip().lower()

    # Admin login is a high-value target: fail CLOSED if Redis is unreachable
    # rather than let rate limiting silently disappear (unlike the regular
    # user-facing auth endpoints in app/routers/auth.py, which fail open).
    check_rate_limit(
        f"ratelimit:admin_login:ipacct:{ip}:{email}",
        limit=10,
        window_seconds=5 * 60,
        fail_open=False,
        detail="Too many login attempts for this account. Please try again in a few minutes.",
    )
    check_rate_limit(
        f"ratelimit:admin_login:ip:{ip}",
        limit=20,
        window_seconds=60 * 60,
        fail_open=False,
        detail="Too many login attempts from this network. Please try again later.",
    )

    _guard_brute_force(ip)

    # Both checks run to prevent enumeration (don't short-circuit on email mismatch)
    bootstrap_email_ok = email == settings.admin_email.strip().lower()
    bootstrap_hash_ok = bool(settings.admin_password_hash) and _verify_password(
        payload.password, settings.admin_password_hash
    )
    is_bootstrap = bootstrap_email_ok and bootstrap_hash_ok

    account: Optional[AdminAccount] = None
    if not is_bootstrap:
        candidate = db.exec(
            select(AdminAccount).where(AdminAccount.email == email, AdminAccount.status == "active")
        ).first()
        if candidate and candidate.password_hash and _verify_password(payload.password, candidate.password_hash):
            account = candidate

    if not is_bootstrap and account is None:
        _record_failure(ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    _clear_failures(ip)

    challenge = secrets.token_urlsafe(48)
    identity = (
        {"kind": "bootstrap"}
        if is_bootstrap
        else {"kind": "account", "account_id": str(account.id)}
    )
    get_redis_client().setex(f"admin:challenge:{challenge}", _CHALLENGE_TTL, json.dumps(identity))

    return {"challenge_token": challenge}


@router.post("/step2", summary="Admin login — step 2: TOTP verification")
def admin_step2(
    payload: Step2Request,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Validate the TOTP code and the challenge token from step 1.
    On success, returns a signed admin JWT.  The challenge token is consumed
    (one-time use) to prevent replay attacks.
    """
    import pyotp

    ip = get_client_ip(request)

    # OTP/TOTP codes are short (6 digits) and brute-forceable if unbounded —
    # limit by both IP and the specific challenge token being attacked.
    check_rate_limit(
        f"ratelimit:admin_step2:ip:{ip}",
        limit=5,
        window_seconds=10 * 60,
        fail_open=False,
        detail="Too many 2FA attempts from this network. Please try again later.",
    )
    check_rate_limit(
        f"ratelimit:admin_step2:token:{payload.challenge_token}",
        limit=5,
        window_seconds=10 * 60,
        fail_open=False,
        detail="Too many 2FA attempts for this login attempt. Please start over.",
    )

    _guard_brute_force(ip)

    redis = get_redis_client()
    challenge_key = f"admin:challenge:{payload.challenge_token}"

    raw_identity = redis.get(challenge_key)
    if not raw_identity:
        _record_failure(ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired challenge token",
        )
    identity = json.loads(raw_identity)

    admin_id: Optional[str] = None
    admin_email = settings.admin_email
    admin_role = "super_admin"
    totp_secret = settings.admin_2fa_secret

    if identity.get("kind") == "account":
        account = db.get(AdminAccount, uuid.UUID(identity["account_id"]))
        if account is None or account.status != "active":
            _record_failure(ip)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account no longer active")
        admin_id = str(account.id)
        admin_email = account.email
        admin_role = account.role
        totp_secret = account.totp_secret

    if not totp_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="2FA is not configured for this account",
        )

    totp = pyotp.TOTP(totp_secret)
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

    if identity.get("kind") == "account":
        from datetime import datetime
        account.last_login_at = datetime.utcnow()
        db.add(account)
        db.commit()

    # Issue admin session JWT
    jti = str(uuid.uuid4())
    expire_seconds = settings.admin_jwt_expire_minutes * 60
    redis.setex(f"admin:session:{jti}", expire_seconds, "1")

    token = create_admin_jwt(jti, admin_id=admin_id, email=admin_email, role=admin_role)
    return {
        "access_token": token,
        "token_type": "bearer",
        "email": admin_email,
        "role": admin_role,
        "expires_in": expire_seconds,
    }


@router.post("/logout", summary="Admin logout — invalidate session")
def admin_logout(request: Request) -> Dict[str, str]:
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
