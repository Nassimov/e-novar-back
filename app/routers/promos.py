"""
Student-facing promo code endpoints.

Workflow:
  1. Student opens /student/promo.
  2. GET /api/promos/  →  list of active codes the user is eligible for.
  3. Student types a code → POST /api/promos/validate  (preview, no side-effects).
  4. Student confirms → POST /api/promos/apply:
       - KP codes   → KP awarded immediately via award_kp().
       - Discount codes → PromoRedemption created (booking_id=NULL);
                         discount is applied when a booking is created with
                         this code (bookings router picks it up).
  5. GET /api/promos/my-redemptions  →  user's history.

Targeting: each code has a `target_role` that restricts eligibility.
See app/services/promo_targeting.py for the full list of values and rules.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import or_
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.admin import PromoCode, PromoRedemption
from app.models.enums import KpSource
from app.services.promo_targeting import build_context, check_eligibility

router = APIRouter(tags=["Promos"])


# ── helpers ──────────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime) -> datetime:
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


def _is_valid(code: PromoCode, now: datetime) -> tuple[bool, str]:
    if not code.active:
        return False, "Ce code est inactif."
    if code.valid_from and _aware(code.valid_from) > now:
        return False, "Ce code n'est pas encore actif."
    if code.valid_to and _aware(code.valid_to) < now:
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
    Return active promo codes the current user is eligible for.
    Pre-fetches targeting data once to avoid N+1 queries.
    """
    uid = UUID(current_user["id"])
    role = current_user.get("role", "student")
    now = _utcnow()

    # Pre-fetch user data needed by all targeting checks (max 3 extra queries)
    ctx = build_context(uid, role, db)

    # IDs already redeemed by this user
    redeemed_ids: set[UUID] = {
        r.code_id
        for r in db.exec(select(PromoRedemption).where(PromoRedemption.user_id == uid)).all()
    }

    # Fetch all currently active codes (date/uses window only — role filtering is Python-side
    # because sub-roles like student_bac require runtime evaluation, not SQL predicates)
    codes = db.exec(
        select(PromoCode)
        .where(
            PromoCode.active == True,
            or_(PromoCode.valid_from == None, PromoCode.valid_from <= now),
            or_(PromoCode.valid_to == None, PromoCode.valid_to >= now),
            or_(PromoCode.max_uses == None, PromoCode.uses < PromoCode.max_uses),
        )
        .order_by(PromoCode.created_at.desc())
    ).all()

    result = []
    for c in codes:
        eligible, _ = check_eligibility(ctx, c.target_role)
        if eligible:
            result.append(_serialize(c, c.id in redeemed_ids))

    return result


@router.post("/validate")
async def validate_code(
    payload: CodeRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Preview a code without applying it.
    Returns code details + eligibility. Raises 4xx on any problem.
    """
    uid = UUID(current_user["id"])
    role = current_user.get("role", "student")
    now = _utcnow()

    code_obj = db.exec(select(PromoCode).where(PromoCode.code == payload.code)).first()
    if code_obj is None:
        raise HTTPException(status_code=404, detail="Code introuvable.")

    valid, reason = _is_valid(code_obj, now)
    if not valid:
        raise HTTPException(status_code=400, detail=reason)

    ctx = build_context(uid, role, db)
    eligible, eligibility_reason = check_eligibility(ctx, code_obj.target_role)
    if not eligible:
        raise HTTPException(status_code=403, detail=eligibility_reason)

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
      the discount is applied when the user creates a booking with this code.
    - Mixed codes (KP + discount): both happen.
    """
    uid = UUID(current_user["id"])
    role = current_user.get("role", "student")
    now = _utcnow()

    code_obj = db.exec(select(PromoCode).where(PromoCode.code == payload.code)).first()
    if code_obj is None:
        raise HTTPException(status_code=404, detail="Code introuvable.")

    valid, reason = _is_valid(code_obj, now)
    if not valid:
        raise HTTPException(status_code=400, detail=reason)

    # Full targeting check — always enforced server-side regardless of what the UI shows
    ctx = build_context(uid, role, db)
    eligible, eligibility_reason = check_eligibility(ctx, code_obj.target_role)
    if not eligible:
        raise HTTPException(status_code=403, detail=eligibility_reason)

    # Idempotency: one redemption per (code, user)
    existing = db.exec(
        select(PromoRedemption).where(
            PromoRedemption.code_id == code_obj.id,
            PromoRedemption.user_id == uid,
        )
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Tu as déjà utilisé ce code.")

    # Increment uses (guard against concurrent exhaustion at the limit)
    code_obj.uses = (code_obj.uses or 0) + 1
    if code_obj.max_uses is not None and code_obj.uses > code_obj.max_uses:
        raise HTTPException(status_code=400, detail="Ce code vient d'atteindre sa limite d'utilisation.")

    db.add(code_obj)
    redemption = PromoRedemption(code_id=code_obj.id, user_id=uid)
    db.add(redemption)

    kp_awarded = 0
    if code_obj.kp_reward and code_obj.kp_reward > 0:
        db.flush()  # ensure redemption row exists before award_kp commits
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
