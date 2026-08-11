from __future__ import annotations

"""
Achievements, Titles, Cosmetics & Player Progression API — Competitive
Arena Phase 14. Mounted under /api/competitive (see app/main.py).

Marking a badge's unlock animation as "seen" already has a generic,
Arena-agnostic endpoint (POST /api/student/badges/{badge_id}/seen,
app/routers/student_badges.py) — reused verbatim by the frontend, not
duplicated here.
"""

from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.arena_cosmetics import ArenaCosmetic
from app.models.gamification import Badge, UserBadge
from app.services.competitive import arena_achievement_service, collection_service, cosmetic_service

router = APIRouter(tags=["Competitive"])


# ─── Achievements ─────────────────────────────────────────────────────────

@router.get("/achievements")
def list_achievements(
    category: Optional[str] = Query(default=None),
    rarity: Optional[str] = Query(default=None),
    completed: Optional[bool] = Query(default=None),
    reward_type: Optional[str] = Query(default=None),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Spec's 'Search' section — filter by Category/Completed/Rarity/Reward
    Type. Hidden achievements are included ONLY once unlocked (spec:
    'Hidden until unlocked')."""
    user_id = UUID(current_user["id"])
    stats = arena_achievement_service.compute_arena_stats(db, user_id)
    owned = {ub.badge_id: ub for ub in db.exec(select(UserBadge).where(UserBadge.user_id == user_id)).all()}

    query = select(Badge).where(Badge.active == True).where(Badge.condition_type.in_(arena_achievement_service.ARENA_CONDITION_TYPES))  # noqa: E712
    if category:
        query = query.where(Badge.category == category)
    if rarity:
        query = query.where(Badge.rarity == rarity)
    badges = list(db.exec(query.order_by(Badge.sort_order.asc(), Badge.condition_threshold.asc())).all())

    items = []
    for b in badges:
        is_unlocked = b.id in owned
        if b.is_hidden and not is_unlocked:
            continue  # secret — never listed until unlocked
        if reward_type:
            has_reward = reward_type == "ep" or any(e.get("reward_type") == reward_type for e in (b.reward_config or []))
            if not has_reward:
                continue
        if completed is not None and completed != is_unlocked:
            continue

        current, total = arena_achievement_service.achievement_progress(b, stats)
        items.append({
            "id": b.id, "name": b.name, "description": b.description, "icon": b.icon, "tier": b.tier,
            "category": b.category, "rarity": b.rarity, "is_hidden": b.is_hidden,
            "ep_reward": b.ep_reward, "reward_config": b.reward_config,
            "unlocked": is_unlocked, "unlocked_at": owned[b.id].unlocked_at if is_unlocked else None,
            "viewed": owned[b.id].viewed_at is not None if is_unlocked else True,
            "progress_current": current, "progress_total": total,
            "progress_pct": round((current / total) * 100, 1) if total else 0.0,
        })
    return {"items": items}


@router.get("/achievements/dashboard")
def get_achievements_dashboard(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Spec's 'Progress Dashboard' — summary counts for a profile view."""
    user_id = UUID(current_user["id"])
    total_arena_badges = db.exec(
        select(Badge).where(Badge.active == True).where(Badge.condition_type.in_(arena_achievement_service.ARENA_CONDITION_TYPES))  # noqa: E712
    ).all()
    unlocked_ids = set(db.exec(
        select(UserBadge.badge_id).where(UserBadge.user_id == user_id)
        .where(UserBadge.badge_id.in_([b.id for b in total_arena_badges]))
    ).all())
    collections = collection_service.list_collections_with_progress(db, user_id)
    return {
        "achievements_unlocked": len(unlocked_ids), "achievements_total": len(total_arena_badges),
        "completion_pct": round((len(unlocked_ids) / len(total_arena_badges)) * 100, 1) if total_arena_badges else 0.0,
        "collections_completed": sum(1 for c in collections if c["completed_at"]), "collections_total": len(collections),
    }


# ─── Collections ──────────────────────────────────────────────────────────

@router.get("/collections")
def get_collections(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    return {"items": collection_service.list_collections_with_progress(db, user_id)}


# ─── Cosmetics catalogue / inventory ────────────────────────────────────────

@router.get("/cosmetics")
def get_cosmetics_catalogue(
    type: Optional[str] = Query(default=None),  # noqa: A002
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = select(ArenaCosmetic).where(ArenaCosmetic.is_active == True).where(ArenaCosmetic.is_hidden == False)  # noqa: E712
    if type:
        query = query.where(ArenaCosmetic.type == type)
    rows = list(db.exec(query.order_by(ArenaCosmetic.category.asc(), ArenaCosmetic.name.asc())).all())
    return {"items": [
        {"id": str(c.id), "type": c.type, "code": c.code, "name": c.name, "description": c.description,
         "icon": c.icon, "asset_url": c.asset_url, "category": c.category, "rarity": c.rarity}
        for c in rows
    ]}


@router.get("/profile/inventory")
def get_my_inventory(
    type: Optional[str] = Query(default=None),  # noqa: A002
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    return {"items": cosmetic_service.list_inventory(db, user_id, cosmetic_type=type)}


@router.get("/profile/showcase")
def get_my_showcase(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    showcase = cosmetic_service.get_or_create_showcase(db, user_id)
    resolved = cosmetic_service.get_public_showcase(db, user_id)
    return {
        **resolved,
        "active_title_id": str(showcase.active_title_id) if showcase.active_title_id else None,
        "active_frame_id": str(showcase.active_frame_id) if showcase.active_frame_id else None,
        "active_banner_id": str(showcase.active_banner_id) if showcase.active_banner_id else None,
        "active_effect_id": str(showcase.active_effect_id) if showcase.active_effect_id else None,
        "pinned_badge_ids": showcase.pinned_badge_ids, "pinned_sticker_ids": [str(s) for s in showcase.pinned_sticker_ids],
        "achievement_showcase_ids": showcase.achievement_showcase_ids,
    }


@router.get("/profile/showcase/{user_id}")
def get_public_showcase(
    user_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return cosmetic_service.get_public_showcase(db, user_id)


class EquipRequest(BaseModel):
    cosmetic_id: Optional[UUID] = None


@router.post("/profile/showcase/equip/{cosmetic_type}")
def equip_cosmetic(
    cosmetic_type: str,
    payload: EquipRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    cosmetic_service.equip_cosmetic(db, user_id, cosmetic_type, payload.cosmetic_id)
    return cosmetic_service.get_public_showcase(db, user_id)


class PinBadgesRequest(BaseModel):
    badge_ids: List[str]


@router.post("/profile/showcase/pin-badges")
def pin_badges(
    payload: PinBadgesRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    cosmetic_service.pin_badges(db, user_id, payload.badge_ids)
    return cosmetic_service.get_public_showcase(db, user_id)


class PinStickersRequest(BaseModel):
    sticker_ids: List[UUID]


@router.post("/profile/showcase/pin-stickers")
def pin_stickers(
    payload: PinStickersRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    cosmetic_service.pin_stickers(db, user_id, payload.sticker_ids)
    return cosmetic_service.get_public_showcase(db, user_id)


class AchievementShowcaseRequest(BaseModel):
    badge_ids: List[str]


@router.post("/profile/showcase/achievement-showcase")
def set_achievement_showcase(
    payload: AchievementShowcaseRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    cosmetic_service.set_achievement_showcase(db, user_id, payload.badge_ids)
    return cosmetic_service.get_public_showcase(db, user_id)
