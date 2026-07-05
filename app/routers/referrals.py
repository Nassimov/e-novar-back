"""
Referral programme endpoints — students and teachers.

Workflow:
  1. User calls GET /my-code  → gets (or creates) their personal referral code.
  2. They share the link: https://<frontend>/r/<code>
  3. New user registers, then calls POST /apply { code }
     → referee gets welcome KP immediately.
  4. When referee completes first session/booking the backend hook calls
     services/referral.validate_referral_for_user() → referrer gets their KP.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.profile import Profile, UserRole
from app.models.referral import Referral
from app.services.referral import (
    REFERRAL_KP,
    REFEREE_KP,
    apply_referral_code,
    get_or_create_code,
)

router = APIRouter(tags=["Referrals"])

# ── helpers ──────────────────────────────────────────────────────────────────

def _me(current_user: Dict[str, Any]) -> UUID:
    return UUID(current_user["id"])


def _role(user_id: UUID, db: Session) -> str:
    row = db.exec(select(UserRole).where(UserRole.user_id == user_id)).first()
    return row.role if row else "student"


def _referral_link(code: str) -> str:
    from app.config import get_settings
    frontend = (get_settings().frontend_url or "https://e-novar.dz").rstrip("/")
    return f"{frontend}/r/{code}"


# ── schemas ───────────────────────────────────────────────────────────────────

class ApplyCodeRequest(BaseModel):
    code: str

    @field_validator("code")
    @classmethod
    def normalise(cls, v: str) -> str:
        return v.strip().upper()


class ReferralItem(BaseModel):
    id: str
    referee_name: str
    referee_role: str
    status: str           # registered | validated
    kp_awarded: int       # KP given to referrer (0 until validated)
    created_at: str
    validated_at: Optional[str]


class MyCodeResponse(BaseModel):
    code: str
    link: str
    rewards: Dict[str, int]   # { referrer_kp, referee_kp }
    stats: Dict[str, int]     # { total, registered, validated, total_kp }
    referrals: List[ReferralItem]


# ── endpoints ─────────────────────────────────────────────────────────────────

@router.get("/my-code", response_model=MyCodeResponse)
async def get_my_referral_code(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get (or generate) the authenticated user's personal referral code and stats."""
    uid = _me(current_user)
    role = _role(uid, db)

    code = get_or_create_code(uid, role, db)
    link = _referral_link(code)

    # Load referral rows where this user is the referrer
    rows = db.exec(
        select(Referral).where(Referral.referrer_id == uid)
    ).all()

    # Enrich with referee names
    referee_ids = [r.referee_id for r in rows if r.referee_id]
    referee_profiles: dict[UUID, Profile] = {}
    if referee_ids:
        for p in db.exec(select(Profile).where(Profile.id.in_(referee_ids))).all():
            referee_profiles[p.id] = p

    items: List[ReferralItem] = []
    for r in sorted(rows, key=lambda x: x.created_at, reverse=True):
        name = "—"
        if r.referee_id and r.referee_id in referee_profiles:
            name = referee_profiles[r.referee_id].full_name or "Utilisateur"
        items.append(ReferralItem(
            id=str(r.id),
            referee_name=name,
            referee_role=r.referee_role,
            status=r.status,
            kp_awarded=r.kp_awarded,
            created_at=r.created_at.isoformat(),
            validated_at=r.validated_at.isoformat() if r.validated_at else None,
        ))

    stats = {
        "total": len(rows),
        "registered": sum(1 for r in rows if r.status == "registered"),
        "validated": sum(1 for r in rows if r.status == "validated"),
        "total_kp": sum(r.kp_awarded for r in rows),
    }

    return MyCodeResponse(
        code=code,
        link=link,
        rewards={"referrer_kp": REFERRAL_KP.get(role, 200), "referee_kp": REFEREE_KP.get(role, 100)},
        stats=stats,
        referrals=items,
    )


@router.post("/apply", status_code=status.HTTP_200_OK)
async def apply_code(
    payload: ApplyCodeRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Apply a referral code.  Call this once after registration/onboarding.
    The caller is the REFEREE (the newly registered user).
    """
    uid = _me(current_user)
    role = _role(uid, db)

    try:
        result = apply_referral_code(uid, role, payload.code, db)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"ok": True, "kp_earned": result["kp_earned"], "referrer_name": result["referrer_name"]}


@router.get("/preview/{code}")
async def preview_code(
    code: str,
    db: Session = Depends(get_db),
):
    """
    Public (unauthenticated) endpoint.
    Returns minimal referrer info so the registration page can show
    'Your friend X invited you — you'll receive N EP'.

    Intentionally returns only name + reward amount.  No IDs, emails, etc.
    Returns 404 if code is invalid so the frontend can validate silently.
    """
    code = code.strip().upper()
    if not code or len(code) > 30:
        raise HTTPException(status_code=404, detail="Code invalide.")

    referrer = db.exec(
        select(Profile).where(Profile.referral_code == code)
    ).first()
    if referrer is None:
        raise HTTPException(status_code=404, detail="Code de parrainage introuvable.")

    role = _role(referrer.id, db)
    return {
        "valid": True,
        "referrer_name": referrer.full_name or "Un ami",
        "referee_kp": REFEREE_KP.get(role, 100),
        "referrer_kp": REFERRAL_KP.get(role, 200),
    }


@router.get("/stats")
async def get_stats(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Summary stats for the current user's referral programme."""
    uid = _me(current_user)
    role = _role(uid, db)

    rows = db.exec(select(Referral).where(Referral.referrer_id == uid)).all()

    return {
        "code": (
            db.exec(select(Profile).where(Profile.id == uid)).first() or Profile()
        ).referral_code,
        "rewards": {"referrer_kp": REFERRAL_KP.get(role, 200), "referee_kp": REFEREE_KP.get(role, 100)},
        "total": len(rows),
        "registered": sum(1 for r in rows if r.status == "registered"),
        "validated": sum(1 for r in rows if r.status == "validated"),
        "total_kp": sum(r.kp_awarded for r in rows),
    }
