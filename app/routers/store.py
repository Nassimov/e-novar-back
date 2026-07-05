from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.profile import Profile
from app.models.store import RewardClaim, StoreItem, UserEntitlement
from app.services.store import redeem_item, format_effect_description
from app.services.effects import expire_stale_entitlements

router = APIRouter(tags=["Store"])

CATEGORY_ORDER = ["powerups", "digital", "physical", "services", "travel"]


@router.get("/items")
async def list_store_items(
    category: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """List active store items, optionally filtered by category."""
    stmt = select(StoreItem).where(StoreItem.active == True)
    if category:
        stmt = stmt.where(StoreItem.category == category)

    items = db.exec(stmt).all()
    # Sort by category order then by name
    cat_rank = {c: i for i, c in enumerate(CATEGORY_ORDER)}
    items.sort(key=lambda x: (cat_rank.get(x.category, 99), x.name))

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


@router.post("/items/{item_id}/redeem", status_code=status.HTTP_201_CREATED)
async def redeem_store_item(
    item_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Exchange EP for a store item. Atomically deducts balance and creates a claim."""
    uid = UUID(current_user["id"])

    # Verify user exists
    user = db.exec(select(Profile).where(Profile.id == uid)).first()
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable.")

    try:
        claim, entitlement, account, msg = redeem_item(uid, item_id, db)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    result: Dict[str, Any] = {
        "message": msg,
        "claim": {
            "id": str(claim.id),
            "item_id": claim.item_id,
            "cost": claim.cost,
            "status": claim.status,
            "claimed_at": claim.claimed_at.isoformat(),
        },
        "new_balance": account.balance,
    }

    if entitlement:
        result["entitlement"] = {
            "effect_type": entitlement.effect_type,
            "status": entitlement.status,
            "expires_at": entitlement.expires_at.isoformat() if entitlement.expires_at else None,
            "description": format_effect_description(entitlement.effect_type, entitlement.effect_config or {}),
        }

    return result


@router.get("/claims")
async def list_my_claims(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List the current user's reward claims with item info."""
    uid = UUID(current_user["id"])

    claims = db.exec(
        select(RewardClaim)
        .where(RewardClaim.user_id == uid)
        .order_by(RewardClaim.claimed_at.desc())
    ).all()

    total = len(claims)
    offset = (page - 1) * size
    page_claims = claims[offset: offset + size]

    # Fetch item names
    item_ids = list({c.item_id for c in page_claims})
    items_map: Dict[str, StoreItem] = {}
    if item_ids:
        for it in db.exec(select(StoreItem).where(StoreItem.id.in_(item_ids))).all():
            items_map[it.id] = it

    return {
        "items": [
            {
                "id": str(c.id),
                "item_id": c.item_id,
                "item_name": items_map[c.item_id].name if c.item_id in items_map else c.item_id,
                "item_icon": items_map[c.item_id].icon if c.item_id in items_map else None,
                "item_category": items_map[c.item_id].category if c.item_id in items_map else None,
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


@router.get("/entitlements")
async def list_my_entitlements(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List the current user's active (non-expired) entitlements."""
    uid = UUID(current_user["id"])
    now = datetime.now(timezone.utc)

    # Sweep expired rows first
    expire_stale_entitlements(uid, db)

    entitlements = db.exec(
        select(UserEntitlement)
        .where(UserEntitlement.user_id == uid, UserEntitlement.status == "active")
        .order_by(UserEntitlement.activated_at.desc())
    ).all()

    return {
        "items": [
            {
                "id": str(e.id),
                "effect_type": e.effect_type,
                "effect_config": e.effect_config,
                "status": e.status,
                "expires_at": e.expires_at.isoformat() if e.expires_at else None,
                "activated_at": e.activated_at.isoformat(),
                "description": format_effect_description(e.effect_type, e.effect_config or {}),
                "is_expired": bool(e.expires_at and e.expires_at < now),
            }
            for e in entitlements
            if not e.expires_at or e.expires_at > now
        ]
    }


@router.get("/entitlements/pdf-pack/files")
async def get_pdf_pack_files(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Return signed download URLs for PDF packs the user has active entitlements for.
    Files must be uploaded by admin to: enovar-files/pdf-packs/{pack_id}/
    Signed URLs are valid for 1 hour.
    """
    uid = UUID(current_user["id"])
    now = datetime.now(timezone.utc)

    active = db.exec(
        select(UserEntitlement).where(
            UserEntitlement.user_id == uid,
            UserEntitlement.effect_type == "pdf_pack",
            UserEntitlement.status == "active",
        )
    ).all()
    active = [e for e in active if not e.expires_at or e.expires_at > now]

    if not active:
        raise HTTPException(status_code=403, detail="Aucun accès Pack PDF actif.")

    from app.services.storage import list_folder, get_signed_url

    packs = []
    seen_pack_ids: set[str] = set()
    for ent in active:
        pack_id = (ent.effect_config or {}).get("pack_id", "bac_10ans")
        if pack_id in seen_pack_ids:
            continue
        seen_pack_ids.add(pack_id)

        folder = f"pdf-packs/{pack_id}"
        files = list_folder(folder)
        signed = []
        for f in files:
            url = get_signed_url(f["path"], expires_in=3600)
            if url:
                signed.append({"filename": f["name"], "url": url})

        packs.append({
            "pack_id": pack_id,
            "entitlement_id": str(ent.id),
            "expires_at": ent.expires_at.isoformat() if ent.expires_at else None,
            "files": signed,
            "file_count": len(signed),
        })

    return {"packs": packs}


@router.get("/entitlements/stickers/files")
async def get_sticker_files(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return signed URLs for all sticker images the user has unlocked."""
    uid = UUID(current_user["id"])
    now = datetime.now(timezone.utc)

    active = db.exec(
        select(UserEntitlement).where(
            UserEntitlement.user_id == uid,
            UserEntitlement.effect_type == "stickers_pack",
            UserEntitlement.status == "active",
        )
    ).all()
    active = [e for e in active if not e.expires_at or e.expires_at > now]

    if not active:
        raise HTTPException(status_code=403, detail="Aucun pack stickers actif.")

    from app.services.storage import list_folder, get_signed_url
    from app.models.profile import Profile

    profile = db.exec(select(Profile).where(Profile.id == uid)).first()
    current_sticker = profile.active_sticker_url if profile else None

    packs = []
    seen: set[str] = set()
    for ent in active:
        pack_id = (ent.effect_config or {}).get("pack_id", "pack1")
        if pack_id in seen:
            continue
        seen.add(pack_id)

        files = list_folder(f"sticker-packs/{pack_id}")
        stickers = []
        for f in files:
            url = get_signed_url(f["path"], expires_in=3600)
            if url:
                stickers.append({"filename": f["name"], "url": url})

        packs.append({
            "pack_id": pack_id,
            "entitlement_id": str(ent.id),
            "stickers": stickers,
            "sticker_count": len(stickers),
        })

    return {"packs": packs, "active_sticker_url": current_sticker}


class _StickerActivateBody(BaseModel):
    sticker_url: str


@router.patch("/entitlements/stickers/activate")
async def activate_sticker(
    body: _StickerActivateBody,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Set (or clear) the student's equipped sticker shown on their profile."""
    uid = UUID(current_user["id"])
    now = datetime.now(timezone.utc)

    # Verify the user actually owns an active stickers_pack
    owned = db.exec(
        select(UserEntitlement).where(
            UserEntitlement.user_id == uid,
            UserEntitlement.effect_type == "stickers_pack",
            UserEntitlement.status == "active",
        )
    ).all()
    if not any(not e.expires_at or e.expires_at > now for e in owned):
        raise HTTPException(status_code=403, detail="Aucun pack stickers actif.")

    from app.models.profile import Profile

    profile = db.exec(select(Profile).where(Profile.id == uid)).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Profil introuvable.")

    # Empty string = unequip sticker
    profile.active_sticker_url = body.sticker_url or None
    db.add(profile)
    db.commit()
    db.refresh(profile)

    return {"active_sticker_url": profile.active_sticker_url}


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
    }
