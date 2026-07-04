from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlmodel import Session, select

from app.dependencies import get_admin_user, get_db
from app.models.profile import Profile
from app.models.store import RewardClaim, StoreItem

router = APIRouter(tags=["Admin — Store"])

VALID_CATEGORIES = {"powerups", "digital", "physical", "services", "travel"}
VALID_EFFECT_TYPES = {
    "ep_boost_2x_24h", "ai_boost_5q", "streak_shield_1d",
    "premium_30d", "pdf_pack_bac", "stickers_pack",
    "coaching_credit", "psychometric_credit",
    "voucher_carrefour", "travel_booking",
}


class StoreItemCreate(BaseModel):
    id: Optional[str] = None          # auto-slug from name if not provided
    category: str
    name: str = Field(min_length=2, max_length=120)
    description: Optional[str] = Field(default=None, max_length=500)
    cost: int = Field(ge=1)
    level_required: int = Field(default=1, ge=1, le=7)
    icon: Optional[str] = Field(default=None, max_length=60)
    badge: Optional[str] = Field(default=None, max_length=30)
    stock: Optional[int] = Field(default=None, ge=0)
    effect_type: Optional[str] = None
    effect_config: Optional[Dict[str, Any]] = None
    active: bool = True

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str) -> str:
        if v not in VALID_CATEGORIES:
            raise ValueError(f"Catégorie invalide. Valeurs autorisées : {sorted(VALID_CATEGORIES)}")
        return v

    @field_validator("effect_type")
    @classmethod
    def validate_effect_type(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in VALID_EFFECT_TYPES:
            raise ValueError(f"effect_type invalide. Valeurs autorisées : {sorted(VALID_EFFECT_TYPES)}")
        return v


class StoreItemUpdate(BaseModel):
    category: Optional[str] = None
    name: Optional[str] = Field(default=None, min_length=2, max_length=120)
    description: Optional[str] = Field(default=None, max_length=500)
    cost: Optional[int] = Field(default=None, ge=1)
    level_required: Optional[int] = Field(default=None, ge=1, le=7)
    icon: Optional[str] = Field(default=None, max_length=60)
    badge: Optional[str] = None
    stock: Optional[int] = Field(default=None, ge=0)
    effect_type: Optional[str] = None
    effect_config: Optional[Dict[str, Any]] = None
    active: Optional[bool] = None

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in VALID_CATEGORIES:
            raise ValueError(f"Catégorie invalide.")
        return v

    @field_validator("effect_type")
    @classmethod
    def validate_effect_type(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v != "" and v not in VALID_EFFECT_TYPES:
            raise ValueError(f"effect_type invalide.")
        return v or None


class ClaimProcessRequest(BaseModel):
    action: str   # "approve", "deliver", "cancel"
    note: Optional[str] = None


def _slugify(text: str) -> str:
    slug = text.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_-]+", "_", slug)
    return slug[:60]


def _item_to_dict(it: StoreItem) -> Dict[str, Any]:
    return {
        "id": it.id,
        "category": it.category,
        "name": it.name,
        "description": it.description,
        "cost": it.cost,
        "level_required": it.level_required,
        "icon": it.icon,
        "badge": it.badge,
        "stock": it.stock,
        "active": it.active,
        "effect_type": it.effect_type,
        "effect_config": it.effect_config,
    }


# ── Items CRUD ─────────────────────────────────────────────────────────────────

@router.get("/items")
async def list_items(
    category: Optional[str] = Query(None),
    active: Optional[bool] = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """List all store items with optional filters."""
    stmt = select(StoreItem)
    if category:
        stmt = stmt.where(StoreItem.category == category)
    if active is not None:
        stmt = stmt.where(StoreItem.active == active)

    items = db.exec(stmt).all()
    items.sort(key=lambda x: (x.category, x.name))

    total = len(items)
    offset = (page - 1) * size
    page_items = items[offset: offset + size]

    return {
        "items": [_item_to_dict(it) for it in page_items],
        "total": total,
        "page": page,
        "size": size,
        "pages": math.ceil(total / size) if total else 0,
    }


@router.post("/items", status_code=status.HTTP_201_CREATED)
async def create_item(
    payload: StoreItemCreate,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Create a new store item."""
    item_id = payload.id or _slugify(payload.name)
    if not item_id:
        raise HTTPException(status_code=400, detail="Impossible de générer un ID pour cet article.")

    if db.get(StoreItem, item_id):
        raise HTTPException(status_code=409, detail=f"Un article avec l'ID '{item_id}' existe déjà.")

    item = StoreItem(
        id=item_id,
        category=payload.category,
        name=payload.name,
        description=payload.description,
        cost=payload.cost,
        level_required=payload.level_required,
        icon=payload.icon,
        badge=payload.badge,
        stock=payload.stock,
        active=payload.active,
        effect_type=payload.effect_type,
        effect_config=payload.effect_config,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return _item_to_dict(item)


@router.put("/items/{item_id}")
async def update_item(
    item_id: str,
    payload: StoreItemUpdate,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Update a store item's fields."""
    item = db.get(StoreItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Article introuvable.")

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(item, field, value)

    db.add(item)
    db.commit()
    db.refresh(item)
    return _item_to_dict(item)


@router.patch("/items/{item_id}/toggle")
async def toggle_item_active(
    item_id: str,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Toggle active/inactive status of a store item."""
    item = db.get(StoreItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Article introuvable.")

    item.active = not item.active
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"id": item.id, "active": item.active}


@router.delete("/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_item(
    item_id: str,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """
    Delete a store item.
    If it has existing claims, soft-deletes (sets active=False).
    If no claims exist, hard-deletes.
    """
    item = db.get(StoreItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Article introuvable.")

    has_claims = db.exec(
        select(RewardClaim).where(RewardClaim.item_id == item_id).limit(1)
    ).first()

    if has_claims:
        # Soft delete — preserve claim FK integrity
        item.active = False
        db.add(item)
        db.commit()
    else:
        db.delete(item)
        db.commit()

    return None


# ── Claims management ──────────────────────────────────────────────────────────

@router.get("/claims")
async def list_claims(
    claim_status: Optional[str] = Query(None, alias="status"),
    category: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """List all reward claims with user and item info."""
    stmt = select(RewardClaim).order_by(RewardClaim.claimed_at.desc())
    if claim_status:
        stmt = stmt.where(RewardClaim.status == claim_status)

    claims = db.exec(stmt).all()

    # Filter by category if provided
    if category:
        item_ids_in_cat = {
            it.id
            for it in db.exec(
                select(StoreItem).where(StoreItem.category == category)
            ).all()
        }
        claims = [c for c in claims if c.item_id in item_ids_in_cat]

    total = len(claims)
    offset = (page - 1) * size
    page_claims = claims[offset: offset + size]

    # Bulk fetch users and items
    user_ids = list({c.user_id for c in page_claims})
    item_ids = list({c.item_id for c in page_claims})
    users_map: Dict[UUID, Profile] = {}
    items_map: Dict[str, StoreItem] = {}
    if user_ids:
        for u in db.exec(select(Profile).where(Profile.id.in_(user_ids))).all():
            users_map[u.id] = u
    if item_ids:
        for it in db.exec(select(StoreItem).where(StoreItem.id.in_(item_ids))).all():
            items_map[it.id] = it

    return {
        "items": [
            {
                "id": str(c.id),
                "user_id": str(c.user_id),
                "user_name": users_map[c.user_id].full_name if c.user_id in users_map else "—",
                "item_id": c.item_id,
                "item_name": items_map[c.item_id].name if c.item_id in items_map else c.item_id,
                "item_category": items_map[c.item_id].category if c.item_id in items_map else None,
                "item_icon": items_map[c.item_id].icon if c.item_id in items_map else None,
                "cost": c.cost,
                "status": c.status,
                "claimed_at": c.claimed_at.isoformat(),
                "processed_at": c.processed_at.isoformat() if c.processed_at else None,
            }
            for c in page_claims
        ],
        "total": total,
        "page": page,
        "size": size,
        "pages": math.ceil(total / size) if total else 0,
    }


@router.put("/claims/{claim_id}/process")
async def process_claim(
    claim_id: UUID,
    payload: ClaimProcessRequest,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """
    Process a pending claim.
    action=approve → status "approved"
    action=deliver → status "delivered"
    action=cancel  → status "cancelled" (EP not refunded automatically — admin handles manually)
    """
    valid_actions = {"approve", "deliver", "cancel"}
    if payload.action not in valid_actions:
        raise HTTPException(status_code=400, detail=f"Action invalide. Valeurs : {valid_actions}")

    claim = db.get(RewardClaim, claim_id)
    if not claim:
        raise HTTPException(status_code=404, detail="Demande introuvable.")

    if claim.status in ("delivered", "cancelled"):
        raise HTTPException(
            status_code=400,
            detail=f"Demande déjà {claim.status} — impossible de la modifier.",
        )

    status_map = {"approve": "approved", "deliver": "delivered", "cancel": "cancelled"}
    claim.status = status_map[payload.action]
    claim.processed_at = datetime.now(timezone.utc)
    db.add(claim)
    db.commit()
    db.refresh(claim)

    return {
        "id": str(claim.id),
        "status": claim.status,
        "processed_at": claim.processed_at.isoformat(),
    }
