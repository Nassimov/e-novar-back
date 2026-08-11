from __future__ import annotations

"""
Cosmetic Inventory & Showcase Manager — Competitive Arena Phase 14.

Single funnel for granting a cosmetic (grant_cosmetic, called from
reward_service.grant_reward's Phase 14 extension AND directly by
arena_achievement_service) and for equipping/pinning it (everything past
that). "Equipped" state lives in ONE row per player (ArenaPlayerShowcase)
— see that model's own docstring for why."""

import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.models.admin import PlatformSettings
from app.models.arena_cosmetics import ArenaCosmetic, ArenaCosmeticInventory, ArenaPlayerShowcase, COSMETIC_TYPES
from app.models.gamification import Badge, UserBadge

logger = logging.getLogger(__name__)


def _settings(db: Session) -> PlatformSettings:
    return db.get(PlatformSettings, True) or PlatformSettings()


def get_or_create_showcase(db: Session, user_id: UUID) -> ArenaPlayerShowcase:
    showcase = db.get(ArenaPlayerShowcase, user_id)
    if showcase is None:
        showcase = ArenaPlayerShowcase(user_id=user_id)
        db.add(showcase)
        db.commit()
        db.refresh(showcase)
    return showcase


def find_cosmetic(db: Session, *, cosmetic_type: str, code: str) -> Optional[ArenaCosmetic]:
    return db.exec(select(ArenaCosmetic).where(ArenaCosmetic.type == cosmetic_type).where(ArenaCosmetic.code == code)).first()


def grant_cosmetic_by_code(db: Session, user_id: UUID, *, cosmetic_type: str, code: str, source: str) -> Optional[ArenaCosmeticInventory]:
    """Best-effort — returns None (and logs) if no catalogue row matches
    `code`, rather than raising (a reward_config typo must never break the
    achievement/reward flow that triggered it)."""
    cosmetic = find_cosmetic(db, cosmetic_type=cosmetic_type, code=code)
    if cosmetic is None:
        logger.warning("cosmetic_service.grant_cosmetic_by_code: no %s cosmetic with code=%s — skipped", cosmetic_type, code)
        return None
    return grant_cosmetic(db, user_id, cosmetic.id, source=source)


def grant_cosmetic(db: Session, user_id: UUID, cosmetic_id: UUID, *, source: str) -> ArenaCosmeticInventory:
    existing = db.exec(
        select(ArenaCosmeticInventory).where(ArenaCosmeticInventory.user_id == user_id).where(ArenaCosmeticInventory.cosmetic_id == cosmetic_id)
    ).first()
    if existing is not None:
        return existing
    entry = ArenaCosmeticInventory(user_id=user_id, cosmetic_id=cosmetic_id, source=source)
    db.add(entry)
    db.commit()
    db.refresh(entry)

    cosmetic = db.get(ArenaCosmetic, cosmetic_id)
    if cosmetic is not None:
        from app.services.notification_engine import emit
        event_type = "arena_title_unlocked" if cosmetic.type == "title" else "arena_cosmetic_unlocked"
        emit(
            db, event_type=event_type, user_id=user_id, context={"cosmetic_name": cosmetic.name},
            data={"cosmetic_id": str(cosmetic.id), "type": cosmetic.type},
            dedup_key=f"{event_type}:{user_id}:{cosmetic.id}",
        )
    return entry


def list_inventory(db: Session, user_id: UUID, *, cosmetic_type: Optional[str] = None) -> List[Dict[str, Any]]:
    query = (
        select(ArenaCosmeticInventory, ArenaCosmetic)
        .join(ArenaCosmetic, ArenaCosmetic.id == ArenaCosmeticInventory.cosmetic_id)
        .where(ArenaCosmeticInventory.user_id == user_id)
    )
    if cosmetic_type:
        query = query.where(ArenaCosmetic.type == cosmetic_type)
    rows = db.exec(query.order_by(ArenaCosmeticInventory.granted_at.desc())).all()
    return [
        {
            "cosmetic_id": str(c.id), "type": c.type, "code": c.code, "name": c.name, "description": c.description,
            "icon": c.icon, "asset_url": c.asset_url, "rarity": c.rarity, "category": c.category,
            "granted_at": inv.granted_at, "source": inv.source,
        }
        for inv, c in rows
    ]


def _owns_cosmetic(db: Session, user_id: UUID, cosmetic_id: UUID) -> bool:
    return db.exec(
        select(ArenaCosmeticInventory).where(ArenaCosmeticInventory.user_id == user_id).where(ArenaCosmeticInventory.cosmetic_id == cosmetic_id)
    ).first() is not None


_SLOT_FIELD = {"title": "active_title_id", "frame": "active_frame_id", "banner": "active_banner_id", "effect": "active_effect_id"}


def equip_cosmetic(db: Session, user_id: UUID, cosmetic_type: str, cosmetic_id: Optional[UUID]) -> ArenaPlayerShowcase:
    """cosmetic_id=None unequips that slot. Only title/frame/banner/effect
    have a single-slot 'equip' concept — badges/stickers are pinned lists
    (see pin_badge/pin_sticker below)."""
    if cosmetic_type not in _SLOT_FIELD:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Type de cosmétique invalide pour l'équipement (options : {list(_SLOT_FIELD)}).")
    showcase = get_or_create_showcase(db, user_id)
    if cosmetic_id is not None:
        if not _owns_cosmetic(db, user_id, cosmetic_id):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tu ne possèdes pas ce cosmétique.")
        cosmetic = db.get(ArenaCosmetic, cosmetic_id)
        if cosmetic is None or cosmetic.type != cosmetic_type:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cosmétique introuvable pour ce type.")
    setattr(showcase, _SLOT_FIELD[cosmetic_type], cosmetic_id)
    from datetime import datetime, timezone
    showcase.updated_at = datetime.now(timezone.utc)
    db.add(showcase)
    db.commit()
    db.refresh(showcase)
    return showcase


def pin_badges(db: Session, user_id: UUID, badge_ids: List[str]) -> ArenaPlayerShowcase:
    settings_row = _settings(db)
    if len(badge_ids) > settings_row.competitive_badge_showcase_max:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Maximum {settings_row.competitive_badge_showcase_max} badges épinglés.")
    owned = set(db.exec(select(UserBadge.badge_id).where(UserBadge.user_id == user_id)).all())
    if not set(badge_ids).issubset(owned):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tu ne possèdes pas tous ces badges.")
    showcase = get_or_create_showcase(db, user_id)
    showcase.pinned_badge_ids = badge_ids
    db.add(showcase)
    db.commit()
    db.refresh(showcase)
    return showcase


def pin_stickers(db: Session, user_id: UUID, sticker_ids: List[UUID]) -> ArenaPlayerShowcase:
    settings_row = _settings(db)
    if len(sticker_ids) > settings_row.competitive_sticker_showcase_max:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Maximum {settings_row.competitive_sticker_showcase_max} stickers épinglés.")
    for sid in sticker_ids:
        if not _owns_cosmetic(db, user_id, sid):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tu ne possèdes pas tous ces stickers.")
    showcase = get_or_create_showcase(db, user_id)
    showcase.pinned_sticker_ids = sticker_ids
    db.add(showcase)
    db.commit()
    db.refresh(showcase)
    return showcase


def set_achievement_showcase(db: Session, user_id: UUID, badge_ids: List[str]) -> ArenaPlayerShowcase:
    settings_row = _settings(db)
    if len(badge_ids) > settings_row.competitive_achievement_showcase_max:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Maximum {settings_row.competitive_achievement_showcase_max} succès mis en avant.")
    owned = set(db.exec(select(UserBadge.badge_id).where(UserBadge.user_id == user_id)).all())
    if not set(badge_ids).issubset(owned):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tu ne possèdes pas tous ces succès.")
    showcase = get_or_create_showcase(db, user_id)
    showcase.achievement_showcase_ids = badge_ids
    db.add(showcase)
    db.commit()
    db.refresh(showcase)
    return showcase


def get_public_showcase(db: Session, user_id: UUID) -> Dict[str, Any]:
    """Read-only, resolved view — used to render another player's profile/
    leaderboard row/replay/chat (spec: 'Displayed: Profile, Leaderboard,
    Battle, Replay, Tournament, Club, Chat')."""
    showcase = db.get(ArenaPlayerShowcase, user_id)
    if showcase is None:
        return {"title": None, "frame": None, "banner": None, "effect": None, "badges": [], "stickers": []}

    cosmetic_ids = [showcase.active_title_id, showcase.active_frame_id, showcase.active_banner_id, showcase.active_effect_id]
    cosmetic_ids = [c for c in cosmetic_ids if c is not None] + list(showcase.pinned_sticker_ids or [])
    cosmetics = {c.id: c for c in db.exec(select(ArenaCosmetic).where(ArenaCosmetic.id.in_(cosmetic_ids)))} if cosmetic_ids else {}

    def _out(cid: Optional[UUID]) -> Optional[Dict[str, Any]]:
        c = cosmetics.get(cid) if cid else None
        return {"id": str(c.id), "code": c.code, "name": c.name, "icon": c.icon, "asset_url": c.asset_url, "rarity": c.rarity} if c else None

    # Phase 14 shipped two badge-pinning concepts (pinned_badge_ids via
    # pin_badges/PATCH profile/showcase/pin-badges, and achievement_
    # showcase_ids via set_achievement_showcase/PATCH profile/showcase/
    # achievement-showcase) but only ever wired the SECOND one to a real
    # frontend control (student.practice.inventory.tsx's "Succès mis en
    # avant" picker) — pinned_badge_ids has no UI that ever writes to it,
    # so sourcing the public 'badges' list from it silently showed nothing
    # no matter what a player picked. achievement_showcase_ids is the one
    # actually driven by the UI, so it's the one that must resolve here.
    badge_ids = list(showcase.achievement_showcase_ids or [])
    badges = {b.id: b for b in db.exec(select(Badge).where(Badge.id.in_(badge_ids)))} if badge_ids else {}

    return {
        "title": _out(showcase.active_title_id), "frame": _out(showcase.active_frame_id),
        "banner": _out(showcase.active_banner_id), "effect": _out(showcase.active_effect_id),
        "badges": [
            {"id": b.id, "name": b.name, "icon": b.icon, "tier": b.tier, "rarity": b.rarity}
            for bid in badge_ids if (b := badges.get(bid)) is not None
        ],
        "stickers": [_out(sid) for sid in (showcase.pinned_sticker_ids or [])],
    }
