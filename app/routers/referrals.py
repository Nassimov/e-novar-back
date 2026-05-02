from __future__ import annotations

import random
import string
from typing import Any, Dict
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.promo import PromoCode, Referral
from app.models.user import User
from app.services.kp import KpSource, award_kp

router = APIRouter(tags=["referrals"])

REFERRAL_KP_REWARD = 200
REFEREE_KP_REWARD = 100


def _generate_referral_code(name: str) -> str:
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    prefix = name.split()[0].upper()[:6] if name else "KARINI"
    return f"{prefix}{suffix}"


@router.get("/code")
async def get_referral_code(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get or create the current user's referral code."""
    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    # Check if referral record already exists
    referral = db.exec(
        select(Referral).where(Referral.referrer_id == user.id, Referral.referee_id.is_(None))
    ).first()

    if referral is None:
        code = _generate_referral_code(user.full_name)
        # Ensure uniqueness
        while db.exec(select(Referral).where(Referral.code == code)).first():
            code = _generate_referral_code(user.full_name)

        referral = Referral(referrer_id=user.id, code=code)
        db.add(referral)
        db.commit()
        db.refresh(referral)

    referral_link = f"https://karini.dz/join?ref={referral.code}"
    return {
        "code": referral.code,
        "link": referral_link,
        "kp_reward_per_referral": REFERRAL_KP_REWARD,
    }


class TrackReferralRequest(BaseModel):
    code: str


@router.post("/track")
async def track_referral(
    payload: TrackReferralRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Track a referral when a new user signs up with a referral code."""
    stmt = select(User).where(User.supabase_id == current_user["id"])
    referee = db.exec(stmt).first()
    if referee is None:
        raise HTTPException(status_code=404, detail="User not found")

    # Find the referral code
    referral = db.exec(select(Referral).where(Referral.code == payload.code)).first()
    if referral is None:
        raise HTTPException(status_code=404, detail="Invalid referral code")

    if referral.referrer_id == referee.id:
        raise HTTPException(status_code=400, detail="Cannot use your own referral code")

    if referral.referee_id is not None:
        raise HTTPException(status_code=409, detail="Referral code already used")

    referral.referee_id = referee.id
    referral.status = "completed"
    referral.kp_awarded = REFERRAL_KP_REWARD
    db.add(referral)
    db.commit()

    # Award KP to both
    award_kp(referral.referrer_id, REFERRAL_KP_REWARD, KpSource.referral, "Parrainage réussi", db)
    award_kp(referee.id, REFEREE_KP_REWARD, KpSource.referral, "Inscription via parrainage", db)

    return {
        "message": "Referral tracked successfully",
        "kp_earned": REFEREE_KP_REWARD,
    }


@router.get("/earnings")
async def get_referral_earnings(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get referral earnings and history."""
    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    referrals = db.exec(
        select(Referral).where(
            Referral.referrer_id == user.id,
            Referral.referee_id.is_not(None),
        )
    ).all()

    total_kp = sum(r.kp_awarded for r in referrals)
    return {
        "total_referrals": len(referrals),
        "total_kp_earned": total_kp,
        "referrals": [
            {
                "id": str(r.id),
                "status": r.status,
                "kp_awarded": r.kp_awarded,
                "created_at": r.created_at.isoformat(),
            }
            for r in referrals
        ],
    }


class PromoValidateRequest(BaseModel):
    code: str
    booking_amount: int


@router.post("/promos/validate")
async def validate_promo_code(
    payload: PromoValidateRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Validate a promo code and calculate discount."""
    from datetime import datetime

    promo = db.exec(
        select(PromoCode).where(
            PromoCode.code == payload.code.upper(),
            PromoCode.is_active == True,
        )
    ).first()

    if promo is None:
        raise HTTPException(status_code=404, detail="Invalid promo code")

    if promo.expires_at and promo.expires_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Promo code has expired")

    if promo.used_count >= promo.max_uses:
        raise HTTPException(status_code=400, detail="Promo code usage limit reached")

    discount = 0
    if promo.discount_percent:
        discount = int(payload.booking_amount * promo.discount_percent / 100)
    elif promo.discount_dzd:
        discount = min(promo.discount_dzd, payload.booking_amount)

    final_amount = payload.booking_amount - discount

    return {
        "valid": True,
        "code": promo.code,
        "discount_percent": promo.discount_percent,
        "discount_dzd": discount,
        "original_amount": payload.booking_amount,
        "final_amount": final_amount,
    }
