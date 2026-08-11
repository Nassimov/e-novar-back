from __future__ import annotations

"""
Collection Manager — Competitive Arena Phase 14.

A collection never duplicates achievement logic — it only checks whether
every badge_id in its list is already owned (public.user_badges), then
grants its own reward_config on top. Called from arena_achievement_service.
check_and_unlock_arena_achievements right after any new unlock (a
collection can only ever newly complete as a direct result of a fresh
achievement unlock, so re-checking on every unlock — rather than polling —
is both correct and cheap)."""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List
from uuid import UUID

from sqlmodel import Session, select

from app.models.arena_cosmetics import ArenaCollection, ArenaCollectionProgress
from app.models.gamification import Badge, UserBadge

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def check_and_complete_collections(db: Session, user_id: UUID) -> List[ArenaCollection]:
    owned = set(db.exec(select(UserBadge.badge_id).where(UserBadge.user_id == user_id)).all())
    collections = list(db.exec(select(ArenaCollection).where(ArenaCollection.is_active == True)).all())  # noqa: E712
    if not collections:
        return []

    already_completed = {
        p.collection_id for p in db.exec(
            select(ArenaCollectionProgress).where(ArenaCollectionProgress.user_id == user_id).where(ArenaCollectionProgress.completed_at.is_not(None))
        ).all()
    }

    newly_completed: List[ArenaCollection] = []
    for collection in collections:
        if collection.id in already_completed or not collection.badge_ids:
            continue
        if not set(collection.badge_ids).issubset(owned):
            continue

        progress = db.exec(
            select(ArenaCollectionProgress).where(ArenaCollectionProgress.user_id == user_id).where(ArenaCollectionProgress.collection_id == collection.id)
        ).first()
        if progress is None:
            progress = ArenaCollectionProgress(user_id=user_id, collection_id=collection.id)
        progress.completed_at = _now()
        db.add(progress)
        newly_completed.append(collection)

    if not newly_completed:
        return []
    db.commit()

    from app.services.competitive import reward_service
    from app.services.notification_engine import emit
    for collection in newly_completed:
        for entry in (collection.reward_config or []):
            reward_type = entry.get("reward_type")
            if not reward_type:
                continue
            try:
                reward_service.grant_reward(
                    db, user_id=user_id, season_id=None, source=f"collection:{collection.id}",
                    reward_type=reward_type, reward_ref=entry.get("reward_ref"), reward_amount=entry.get("reward_amount"), notify=False,
                )
            except Exception:
                logger.exception("collection reward grant failed user=%s collection=%s", user_id, collection.id)
        emit(
            db, event_type="arena_collection_completed", user_id=user_id, context={"collection_name": collection.name},
            data={"collection_id": str(collection.id)}, dedup_key=f"arena_collection_completed:{user_id}:{collection.id}",
        )
    return newly_completed


def list_collections_with_progress(db: Session, user_id: UUID) -> List[Dict[str, Any]]:
    owned = set(db.exec(select(UserBadge.badge_id).where(UserBadge.user_id == user_id)).all())
    collections = list(db.exec(select(ArenaCollection).where(ArenaCollection.is_active == True)).all())  # noqa: E712
    completed_ids = {
        p.collection_id: p.completed_at for p in db.exec(
            select(ArenaCollectionProgress).where(ArenaCollectionProgress.user_id == user_id)
        ).all()
    }
    out = []
    for c in collections:
        owned_count = len(set(c.badge_ids) & owned)
        out.append({
            "id": str(c.id), "code": c.code, "name": c.name, "description": c.description, "icon": c.icon,
            "total": len(c.badge_ids), "owned": owned_count,
            "completed_at": completed_ids.get(c.id),
        })
    return out
