from __future__ import annotations

import math
from typing import Any, Dict
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session, select

from app.dependencies import get_db, require_role
from app.models.promo import PromoCode
from app.schemas.admin import PromoCodeCreate, PromoCodeUpdate

router = APIRouter(tags=["admin-promos"])

admin_required = require_role("admin")


@router.get("/")
async def list_promo_codes(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    is_active: bool = Query(None),
    current_user: Dict[str, Any] = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """List all promo codes."""
    query = select(PromoCode)
    if is_active is not None:
        query = query.where(PromoCode.is_active == is_active)

    promos = db.exec(query.order_by(PromoCode.created_at.desc())).all()
    total = len(promos)
    offset = (page - 1) * size
    paginated = promos[offset: offset + size]

    return {
        "items": [
            {
                "id": str(p.id),
                "code": p.code,
                "discount_percent": p.discount_percent,
                "discount_dzd": p.discount_dzd,
                "max_uses": p.max_uses,
                "used_count": p.used_count,
                "expires_at": p.expires_at.isoformat() if p.expires_at else None,
                "is_active": p.is_active,
                "created_at": p.created_at.isoformat(),
            }
            for p in paginated
        ],
        "total": total,
        "page": page,
        "size": size,
        "pages": math.ceil(total / size) if total else 0,
    }


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_promo_code(
    payload: PromoCodeCreate,
    current_user: Dict[str, Any] = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Create a new promo code."""
    existing = db.exec(
        select(PromoCode).where(PromoCode.code == payload.code.upper())
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Promo code already exists")

    promo = PromoCode(
        code=payload.code.upper(),
        discount_percent=payload.discount_percent,
        discount_dzd=payload.discount_dzd,
        max_uses=payload.max_uses,
        expires_at=payload.expires_at,
    )
    db.add(promo)
    db.commit()
    db.refresh(promo)

    return {
        "id": str(promo.id),
        "code": promo.code,
        "discount_percent": promo.discount_percent,
        "discount_dzd": promo.discount_dzd,
        "max_uses": promo.max_uses,
        "expires_at": promo.expires_at.isoformat() if promo.expires_at else None,
        "is_active": promo.is_active,
    }


@router.put("/{promo_id}")
async def update_promo_code(
    promo_id: UUID,
    payload: PromoCodeUpdate,
    current_user: Dict[str, Any] = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Update a promo code."""
    promo = db.get(PromoCode, promo_id)
    if promo is None:
        raise HTTPException(status_code=404, detail="Promo code not found")

    if payload.discount_percent is not None:
        promo.discount_percent = payload.discount_percent
    if payload.discount_dzd is not None:
        promo.discount_dzd = payload.discount_dzd
    if payload.max_uses is not None:
        promo.max_uses = payload.max_uses
    if payload.expires_at is not None:
        promo.expires_at = payload.expires_at
    if payload.is_active is not None:
        promo.is_active = payload.is_active

    db.add(promo)
    db.commit()
    db.refresh(promo)
    return {"message": "Promo code updated", "code": promo.code}


@router.delete("/{promo_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_promo_code(
    promo_id: UUID,
    current_user: Dict[str, Any] = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Delete a promo code."""
    promo = db.get(PromoCode, promo_id)
    if promo is None:
        raise HTTPException(status_code=404, detail="Promo code not found")

    db.delete(promo)
    db.commit()
    return None
