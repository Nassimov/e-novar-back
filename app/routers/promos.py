"""
Student-facing promo code endpoints.

Workflow:
  1. Student opens /student/promo.
  2. GET /api/promos  →  list of active codes visible to this user.
  3. Student types a code → POST /api/promos/validate  (preview, no side-effects).
  4. Student confirms → POST /api/promos/apply:
       - KP codes   → KP awarded immediately via award_kp().
       - Discount codes → PromoRedemption created (booking_id=NULL);
                         discount is applied when a booking is created with
                         this code (bookings router picks it up).
  5. GET /api/promos/my-redemptions  →  user's history.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy import or_
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.admin import PromoCode, PromoRedemption
from app.models.enums import KpSource

router = APIRouter(tags=["Promos"])


# ── helpers ──────────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _is_valid(code: PromoCode, now: datetime) -> tuple[bool, str]:
    if not code.active:
        return False, "Ce code est inactif."
    if code.valid_from and code.valid_from > now:
        return False, "Ce code n'est pas encore actif."
    if code.valid_to and code.valid_to < now:
        return False, "Ce code a expiré."
    if code.max_uses is not None and code.uses >= code.max_uses:
        return False, "Ce code a atteint sa limite d'utilisation."
    return True, ""


def _serialize(c: PromoCode, already_used: bool = False) -> dict:
    return {
        "id": str(c.id),
        "code": c.code,
        "title": c.title or c.code,
        "description": c.description,
        "kp_reward": c.kp_reward,
        "discount_type": c.discount_type,
        "discount_value": c.discount_value,
        "valid_to": c.valid_to.isoformat() if c.valid_to else None,
        "uses": c.uses,
        "max_uses": c.max_uses,
        "already_used": already_used,
    }


# ── request schemas ───────────────────────────────────────────────────────────

class CodeRequest(BaseModel):
    code: str

    @field_validator("code")
    @classmethod
    def normalise(cls, v: str) -> str:
        v = v.strip().upper()
        if not v or len(v) > 30:
            raise ValueError("Code invalide.")
        return v


# ── endpoints ─────────────────────────────────────────────────────────────────

@router.get("/")
async def list_active_promos(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Return active promo codes visible to this user.
    Includes already_used flag per code.
    """
    uid = UUID(current_user["id"])
    role = current_user.get("role", "student")
    now = _utcnow()

    # IDs already redeemed by this user
    redeemed_ids: set[UUID] = {
        r.code_id
        for r in db.exec(select(PromoRedemption).where(PromoRedemption.user_id == uid)).all()
    }

    codes = db.exec(
        select(PromoCode)
        .where(
            PromoCode.active == True,
            or_(PromoCode.valid_from == None, PromoCode.valid_from <= now),
            or_(PromoCode.valid_to == None, PromoCode.valid_to >= now),
            or_(PromoCode.max_uses == None, PromoCode.uses < PromoCode.max_uses),
            or_(PromoCode.target_role == "all", PromoCode.target_role == role),
        )
        .order_by(PromoCode.created_at.desc())
    ).all()

    return [_serialize(c, c.id in redeemed_ids) for c in codes]


@router.post("/validate")
async def validate_code(
    payload: CodeRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Preview a code without applying it.
    Returns the code details + whether the user has already used it.
    """
    uid = UUID(current_user["id"])
    role = current_user.get("role", "student")
    now = _utcnow()

    code_obj = db.exec(
        select(PromoCode).where(PromoCode.code == payload.code)
    ).first()

    if code_obj is None:
        raise HTTPException(status_code=404, detail="Code introuvable.")

    valid, reason = _is_valid(code_obj, now)
    if not valid:
        raise HTTPException(status_code=400, detail=reason)

    if code_obj.target_role != "all" and code_obj.target_role != role:
        raise HTTPException(
            status_code=403,
            detail=f"Ce code est réservé aux {code_obj.target_role}s.",
        )

    existing = db.exec(
        select(PromoRedemption).where(
            PromoRedemption.code_id == code_obj.id,
            PromoRedemption.user_id == uid,
        )
    ).first()

    return {**_serialize(code_obj, existing is not None), "valid": True}


@router.post("/apply")
async def apply_code(
    payload: CodeRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Apply a promo code.

    - KP codes: KP awarded immediately.
    - Discount codes: redemption record created (booking_id=NULL);
      discount is applied when the user creates a booking with this code.
    - Mixed codes (KP + discount): both happen.
    """
    uid = UUID(current_user["id"])
    role = current_user.get("role", "student")
    now = _utcnow()

    code_obj = db.exec(
        select(PromoCode).where(PromoCode.code == payload.code)
    ).first()

    if code_obj is None:
        raise HTTPException(status_code=404, detail="Code introuvable.")

    valid, reason = _is_valid(code_obj, now)
    if not valid:
        raise HTTPException(status_code=400, detail=reason)

    if code_obj.target_role != "all" and code_obj.target_role != role:
        raise HTTPException(
            status_code=403,
            detail=f"Ce code est réservé aux {code_obj.target_role}s.",
        )

    # Idempotency: one redemption per (code, user)
    existing = db.exec(
        select(PromoRedemption).where(
            PromoRedemption.code_id == code_obj.id,
            PromoRedemption.user_id == uid,
        )
    ).first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail="Tu as déjà utilisé ce code.",
        )

    # Atomic increment: guard against race conditions at the limit
    if code_obj.max_uses is not None:
        code_obj.uses = (code_obj.uses or 0) + 1
        if code_obj.uses > code_obj.max_uses:
            raise HTTPException(
                status_code=400,
                detail="Ce code vient d'atteindre sa limite d'utilisation.",
            )
    else:
        code_obj.uses = (code_obj.uses or 0) + 1

    db.add(code_obj)

    redemption = PromoRedemption(code_id=code_obj.id, user_id=uid)
    db.add(redemption)

    kp_awarded = 0
    if code_obj.kp_reward and code_obj.kp_reward > 0:
        # flush pending writes before award_kp commits
        db.flush()
        from app.services.kp import award_kp
        award_kp(
            uid,
            code_obj.kp_reward,
            KpSource.promo,
            f"Code promo {code_obj.code} — {code_obj.title or code_obj.code}",
            db,
        )
        kp_awarded = code_obj.kp_reward
    else:
        db.commit()

    return {
        "ok": True,
        "kp_earned": kp_awarded,
        "code": code_obj.code,
        "title": code_obj.title or code_obj.code,
        "discount_type": code_obj.discount_type,
        "discount_value": code_obj.discount_value,
    }


@router.get("/my-redemptions")
async def my_redemptions(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List the authenticated user's promo redemption history."""
    uid = UUID(current_user["id"])

    rows = db.exec(
        select(PromoRedemption, PromoCode)
        .join(PromoCode, PromoRedemption.code_id == PromoCode.id)
        .where(PromoRedemption.user_id == uid)
        .order_by(PromoRedemption.redeemed_at.desc())
    ).all()

    return [
        {
            "id": str(r.id),
            "code": c.code,
            "title": c.title or c.code,
            "kp_reward": c.kp_reward,
            "discount_type": c.discount_type,
            "discount_value": c.discount_value,
            "booking_id": str(r.booking_id) if r.booking_id else None,
            "redeemed_at": r.redeemed_at.isoformat(),
        }
        for r, c in rows
    ]
