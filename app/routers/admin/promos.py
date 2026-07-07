from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlmodel import Session, select

from app.dependencies import get_admin_user, get_db
from app.models.admin import PromoCode, PromoRedemption
from app.schemas.admin import PromoCodeCreate, PromoCodeUpdate

router = APIRouter(tags=["admin-promos"])


def _serialize(p: PromoCode) -> dict:
    return {
        "id": str(p.id),
        "code": p.code,
        "title": p.title or p.code,
        "description": p.description,
        "kp_reward": p.kp_reward,
        "discount_type": p.discount_type,
        "discount_value": p.discount_value,
        "valid_from": p.valid_from.isoformat() if p.valid_from else None,
        "valid_to": p.valid_to.isoformat() if p.valid_to else None,
        "max_uses": p.max_uses,
        "uses": p.uses,
        "active": p.active,
        "target_role": p.target_role,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


def _validate_payload(kp_reward: int, discount_type: str | None, discount_value: int) -> None:
    if kp_reward == 0 and discount_type is None:
        raise HTTPException(
            status_code=400,
            detail="Le code doit avoir au moins une récompense : bonus EP ou réduction.",
        )
    if discount_type is not None and discount_type not in ("percent", "fixed"):
        raise HTTPException(status_code=400, detail="discount_type doit être 'percent' ou 'fixed'.")
    if discount_type == "percent" and not (1 <= discount_value <= 100):
        raise HTTPException(status_code=400, detail="discount_value doit être entre 1 et 100 pour 'percent'.")
    if discount_type == "fixed" and discount_value <= 0:
        raise HTTPException(status_code=400, detail="discount_value doit être > 0 pour 'fixed'.")


@router.get("/stats")
async def get_promo_stats(
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Global promo code overview stats for the admin dashboard."""
    now = datetime.now(timezone.utc)
    codes = db.exec(select(PromoCode)).all()

    def _aware(dt: datetime) -> datetime:
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

    active_count = sum(
        1 for c in codes
        if c.active
        and (c.valid_from is None or _aware(c.valid_from) <= now)
        and (c.valid_to is None or _aware(c.valid_to) >= now)
        and (c.max_uses is None or c.uses < c.max_uses)
    )
    total_uses = sum(c.uses for c in codes)
    total_kp = sum(c.kp_reward * c.uses for c in codes if c.kp_reward)

    return {
        "total": len(codes),
        "active": active_count,
        "total_uses": total_uses,
        "total_kp_awarded": total_kp,
    }


@router.get("/")
async def list_promo_codes(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    active: bool = Query(None),
    search: str = Query(None),
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """List all promo codes (admin)."""
    q = select(PromoCode)
    if active is not None:
        q = q.where(PromoCode.active == active)
    if search:
        term = f"%{search.upper()}%"
        q = q.where(
            (PromoCode.code.ilike(term)) | (PromoCode.title.ilike(f"%{search}%"))
        )
    codes = db.exec(q.order_by(PromoCode.created_at.desc())).all()

    total = len(codes)
    offset = (page - 1) * size
    paginated = codes[offset: offset + size]

    return {
        "items": [_serialize(c) for c in paginated],
        "total": total,
        "page": page,
        "size": size,
        "pages": math.ceil(total / size) if total else 0,
    }


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_promo_code(
    payload: PromoCodeCreate,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Create a new promo code."""
    code_upper = payload.code.strip().upper()

    existing = db.exec(select(PromoCode).where(PromoCode.code == code_upper)).first()
    if existing:
        raise HTTPException(status_code=409, detail="Ce code existe déjà.")

    _validate_payload(payload.kp_reward, payload.discount_type, payload.discount_value)

    if payload.target_role not in ("all", "student", "teacher", "parent"):
        raise HTTPException(status_code=400, detail="target_role invalide.")

    if payload.valid_from and payload.valid_to and payload.valid_from >= payload.valid_to:
        raise HTTPException(status_code=400, detail="valid_from doit être avant valid_to.")

    promo = PromoCode(
        code=code_upper,
        title=payload.title,
        description=payload.description,
        kp_reward=payload.kp_reward,
        discount_type=payload.discount_type,
        discount_value=payload.discount_value,
        valid_from=payload.valid_from,
        valid_to=payload.valid_to,
        max_uses=payload.max_uses,
        target_role=payload.target_role,
        active=payload.active,
        uses=0,
    )
    db.add(promo)
    db.commit()
    db.refresh(promo)
    return _serialize(promo)


@router.get("/{promo_id}/stats")
async def get_code_stats(
    promo_id: UUID,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Per-code usage analytics."""
    promo = db.get(PromoCode, promo_id)
    if promo is None:
        raise HTTPException(status_code=404, detail="Code introuvable.")

    total_redemptions = db.exec(
        select(func.count()).where(PromoRedemption.code_id == promo_id)
    ).first() or 0

    with_booking = db.exec(
        select(func.count()).where(
            PromoRedemption.code_id == promo_id,
            PromoRedemption.booking_id != None,  # noqa: E711
        )
    ).first() or 0

    return {
        "code": promo.code,
        "total_redemptions": total_redemptions,
        "with_booking": with_booking,
        "kp_awarded": promo.kp_reward * total_redemptions,
        "uses": promo.uses,
        "max_uses": promo.max_uses,
        "fill_rate": round(promo.uses / promo.max_uses * 100, 1) if promo.max_uses else None,
    }


@router.patch("/{promo_id}")
async def update_promo_code(
    promo_id: UUID,
    payload: PromoCodeUpdate,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Partially update a promo code (PATCH — only send changed fields)."""
    promo = db.get(PromoCode, promo_id)
    if promo is None:
        raise HTTPException(status_code=404, detail="Code introuvable.")

    if payload.title is not None:
        promo.title = payload.title
    if payload.description is not None:
        promo.description = payload.description
    if payload.kp_reward is not None:
        promo.kp_reward = payload.kp_reward
    if payload.discount_type is not None:
        promo.discount_type = payload.discount_type
    if payload.discount_value is not None:
        promo.discount_value = payload.discount_value
    if payload.valid_from is not None:
        promo.valid_from = payload.valid_from
    if payload.valid_to is not None:
        promo.valid_to = payload.valid_to
    if payload.max_uses is not None:
        promo.max_uses = payload.max_uses
    if payload.target_role is not None:
        if payload.target_role not in ("all", "student", "teacher", "parent"):
            raise HTTPException(status_code=400, detail="target_role invalide.")
        promo.target_role = payload.target_role
    if payload.active is not None:
        promo.active = payload.active

    _validate_payload(promo.kp_reward, promo.discount_type, promo.discount_value)

    db.add(promo)
    db.commit()
    db.refresh(promo)
    return _serialize(promo)


@router.delete("/{promo_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_promo_code(
    promo_id: UUID,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Delete a promo code (also cascades promo_redemptions via DB FK)."""
    promo = db.get(PromoCode, promo_id)
    if promo is None:
        raise HTTPException(status_code=404, detail="Code introuvable.")
    db.delete(promo)
    db.commit()
    return None
