from __future__ import annotations

"""
Account-security endpoints backing the (previously 100% mocked in the
frontend) teacher/student/parent settings pages: real password change, real
TOTP 2FA (mirrors the admin panel's pyotp flow), a real recent-devices /
login-history view backed by login_events, and scheduled account deletion
(60-day grace period, cancelable — see app/workers/account_tasks.py for the
job that actually hard-deletes once the window elapses).
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlmodel import Session, select

from app.core.rate_limit import check_rate_limit, get_client_ip
from app.dependencies import get_current_user, get_db, security
from app.models.account_security import LoginEvent
from app.models.booking import Booking
from app.models.payment import TeacherPayout
from app.models.profile import Profile, TeacherProfile
from app.services import auth as auth_service

router = APIRouter(tags=["Account Security"])

DELETION_GRACE_DAYS = 60


def _get_profile(db: Session, uid: UUID) -> Profile:
    profile = db.get(Profile, uid)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile


# ── Password ─────────────────────────────────────────────────────────────────

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@router.post("/password")
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if len(payload.new_password) < 8:
        raise HTTPException(status_code=422, detail="New password must be at least 8 characters")

    ip = get_client_ip(request)
    check_rate_limit(
        f"ratelimit:change_password:ipacct:{ip}:{current_user['id']}",
        limit=5,
        window_seconds=15 * 60,
        detail="Too many password change attempts. Please try again later.",
    )

    uid = UUID(current_user["id"])
    profile = _get_profile(db, uid)

    try:
        auth_service.login_with_supabase(profile.email or "", payload.current_password)
    except Exception:
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    from app.database import get_supabase_service
    get_supabase_service().auth.admin.update_user_by_id(str(uid), {"password": payload.new_password})

    profile.password_changed_at = datetime.utcnow()
    db.add(profile)
    db.commit()

    return {"message": "Password updated", "password_changed_at": profile.password_changed_at.isoformat()}


# ── 2FA (TOTP) ───────────────────────────────────────────────────────────────

class TotpConfirmRequest(BaseModel):
    code: str


class TotpDisableRequest(BaseModel):
    password: str


@router.post("/2fa/setup")
def setup_totp(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Generates (or regenerates, if setup was abandoned mid-way) a TOTP
    secret. Not active yet — stays off until /2fa/confirm verifies one real
    code, same anti-half-setup rule as the admin panel's enrollment flow."""
    uid = UUID(current_user["id"])
    profile = _get_profile(db, uid)
    if profile.totp_enabled:
        raise HTTPException(status_code=409, detail="2FA is already enabled")

    secret = auth_service.generate_totp_secret()
    profile.totp_secret = secret
    db.add(profile)
    db.commit()

    return {
        "secret": secret,
        "provisioning_uri": auth_service.totp_provisioning_uri(secret, profile.email or ""),
    }


@router.post("/2fa/confirm")
def confirm_totp(
    payload: TotpConfirmRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])
    profile = _get_profile(db, uid)
    if not profile.totp_secret:
        raise HTTPException(status_code=400, detail="Call /2fa/setup first")
    if not auth_service.verify_totp_code(profile.totp_secret, payload.code):
        raise HTTPException(status_code=401, detail="Invalid code")

    profile.totp_enabled = True
    db.add(profile)
    db.commit()
    return {"totp_enabled": True}


@router.post("/2fa/disable")
def disable_totp(
    payload: TotpDisableRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])
    profile = _get_profile(db, uid)
    try:
        auth_service.login_with_supabase(profile.email or "", payload.password)
    except Exception:
        raise HTTPException(status_code=401, detail="Incorrect password")

    profile.totp_enabled = False
    profile.totp_secret = None
    db.add(profile)
    db.commit()
    return {"totp_enabled": False}


# ── Sessions / login history ────────────────────────────────────────────────

class SessionDeviceOut(BaseModel):
    device_label: str
    ip: Optional[str]
    last_seen_at: str
    current: bool


class LoginHistoryItemOut(BaseModel):
    success: bool
    ip: Optional[str]
    device_label: str
    created_at: str


def _device_label(user_agent: Optional[str]) -> str:
    """Cheap, dependency-free UA -> human label. Good enough for a settings
    list; not meant to be a full UA parser."""
    ua = (user_agent or "").lower()
    if "iphone" in ua or "ipad" in ua:
        device = "iPhone/iPad"
    elif "android" in ua:
        device = "Android"
    elif "macintosh" in ua or "mac os" in ua:
        device = "Mac"
    elif "windows" in ua:
        device = "Windows"
    elif "linux" in ua:
        device = "Linux"
    else:
        device = "Appareil inconnu"

    if "edg/" in ua:
        browser = "Edge"
    elif "chrome/" in ua:
        browser = "Chrome"
    elif "firefox/" in ua:
        browser = "Firefox"
    elif "safari/" in ua:
        browser = "Safari"
    else:
        browser = ""
    return f"{device} · {browser}" if browser else device


@router.get("/sessions", response_model=List[SessionDeviceOut])
def list_sessions(
    request: Request,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Recent distinct devices this account has signed in from (deduped by
    IP+user-agent, most recent successful sign-in per device). Not a live
    session list — Supabase's admin API exposes revocation but not
    enumeration of a user's active sessions, see /sessions/revoke-others."""
    uid = UUID(current_user["id"])
    rows = db.exec(
        select(LoginEvent)
        .where(LoginEvent.user_id == uid, LoginEvent.success == True)  # noqa: E712
        .order_by(LoginEvent.created_at.desc())
        .limit(200)
    ).all()

    this_ip = get_client_ip(request)
    this_ua = request.headers.get("User-Agent") or ""

    seen: Dict[tuple, LoginEvent] = {}
    for row in rows:
        key = (row.ip, row.user_agent)
        if key not in seen:
            seen[key] = row
    devices = sorted(seen.values(), key=lambda r: r.created_at, reverse=True)[:10]

    return [
        SessionDeviceOut(
            device_label=_device_label(d.user_agent),
            ip=d.ip,
            last_seen_at=d.created_at.isoformat(),
            current=(d.ip == this_ip and d.user_agent == this_ua),
        )
        for d in devices
    ]


@router.post("/sessions/revoke-others")
def revoke_other_sessions(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Signs out every OTHER active session for this account, keeping the
    current one alive. Supabase's admin sign_out only supports 'global' (kill
    everything including this one) / 'local' / 'others' scopes tied to the
    JWT passed in — there is no per-device selection available, so "kick just
    that one device" isn't an operation the platform can offer; this is the
    honest equivalent for "I think my account is compromised, cut everyone
    else off"."""
    from app.database import get_supabase_service
    try:
        get_supabase_service().auth.admin.sign_out(credentials.credentials, "others")
    except Exception:
        raise HTTPException(status_code=502, detail="Could not reach the auth provider — please try again")
    return {"message": "Other sessions signed out"}


@router.get("/login-history", response_model=List[LoginHistoryItemOut])
def login_history(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])
    rows = db.exec(
        select(LoginEvent)
        .where(LoginEvent.user_id == uid)
        .order_by(LoginEvent.created_at.desc())
        .limit(50)
    ).all()
    return [
        LoginHistoryItemOut(
            success=r.success,
            ip=r.ip,
            device_label=_device_label(r.user_agent),
            created_at=r.created_at.isoformat(),
        )
        for r in rows
    ]


# ── Scheduled account deletion ──────────────────────────────────────────────

class ScheduleDeletionOut(BaseModel):
    deletion_scheduled_for: str


def _blocking_reasons(db: Session, uid: UUID, role: str) -> List[str]:
    """Business rules: deletion must never silently forfeit money owed to
    (teacher) or by (student, via an unresolved booking) either party."""
    reasons: List[str] = []

    upcoming_as_student = db.exec(
        select(Booking).where(Booking.student_id == uid, Booking.status.in_(["pending", "confirmed"]))
    ).first()
    if upcoming_as_student is not None:
        reasons.append("upcoming_booking")

    if role == "teacher":
        upcoming_as_teacher = db.exec(
            select(Booking).where(Booking.teacher_id == uid, Booking.status.in_(["pending", "confirmed"]))
        ).first()
        if upcoming_as_teacher is not None:
            reasons.append("upcoming_booking")

        pending_payout = db.exec(
            select(TeacherPayout).where(TeacherPayout.teacher_id == uid, TeacherPayout.status == "pending")
        ).first()
        if pending_payout is not None:
            reasons.append("pending_payout")

        tp = db.exec(select(TeacherProfile).where(TeacherProfile.user_id == uid)).first()
        if tp is not None and tp.wallet_balance_dzd > 0:
            reasons.append("wallet_balance")

    # de-dupe while preserving order
    return list(dict.fromkeys(reasons))


@router.post("/delete", response_model=ScheduleDeletionOut)
def schedule_account_deletion(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])
    role = current_user.get("role", "student")
    profile = _get_profile(db, uid)

    if profile.deletion_scheduled_for is not None:
        return ScheduleDeletionOut(deletion_scheduled_for=profile.deletion_scheduled_for.isoformat())

    reasons = _blocking_reasons(db, uid, role)
    if reasons:
        raise HTTPException(
            status_code=409,
            detail={"message": "Cannot delete account: unresolved obligations", "reasons": reasons},
        )

    now = datetime.utcnow()
    profile.deletion_requested_at = now
    profile.deletion_scheduled_for = now + timedelta(days=DELETION_GRACE_DAYS)
    db.add(profile)
    db.commit()
    db.refresh(profile)

    # Best-effort: sign out every session immediately — the account is
    # deactivated from this point (the frontend gates all routes on
    # deletion_scheduled_for until it's cancelled), not just at the end of
    # the grace period.
    try:
        from app.database import get_supabase_service
        get_supabase_service().auth.admin.sign_out(credentials.credentials, "global")
    except Exception:
        pass

    return ScheduleDeletionOut(deletion_scheduled_for=profile.deletion_scheduled_for.isoformat())


@router.post("/delete/cancel")
def cancel_account_deletion(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])
    profile = _get_profile(db, uid)
    profile.deletion_requested_at = None
    profile.deletion_scheduled_for = None
    db.add(profile)
    db.commit()
    return {"message": "Account deletion cancelled"}
