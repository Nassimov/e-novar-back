from __future__ import annotations

"""
Club Feed Service — Competitive Arena Phase 11, Part B.

Records the member-facing, permanent activity feed (ClubFeedEvent) —
complementary to, never a replacement for, club_service.log_event's
AuditLog trail (admin-facing audit vs. member-facing feed, see
app/models/club.py's ClubFeedEvent docstring). Called alongside every action
that should show up on a club's public activity feed per spec ("New member,
Promotion, Battle won, Battle lost, Achievement unlocked, New season,
Tournament").

Deliberately fire-and-forget (never raises) — a feed write failing must
never break the action that triggered it, same posture as club_service.
log_event.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlmodel import Session, select

from app.models.club import Club, ClubFeedEvent
from app.models.profile import Profile

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def record(db: Session, *, club_id: UUID, event_type: str, actor_id: Optional[UUID] = None, data: Optional[Dict[str, Any]] = None) -> None:
    try:
        db.add(ClubFeedEvent(club_id=club_id, event_type=event_type, actor_id=actor_id, data=data or {}))
        club = db.get(Club, club_id)
        if club is not None:
            club.last_activity_at = _now()
            db.add(club)
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.exception("club feed record failed club_id=%s event_type=%s", club_id, event_type)


def list_feed(db: Session, club_id: UUID, *, page: int = 1, size: int = 30) -> Dict[str, Any]:
    query = select(ClubFeedEvent).where(ClubFeedEvent.club_id == club_id).order_by(ClubFeedEvent.created_at.desc())
    rows = list(db.exec(query.offset((page - 1) * size).limit(size)).all())
    actor_ids = [r.actor_id for r in rows if r.actor_id is not None]
    profiles = {p.id: p for p in db.exec(select(Profile).where(Profile.id.in_(actor_ids))).all()} if actor_ids else {}
    items: List[Dict[str, Any]] = []
    for r in rows:
        actor = profiles.get(r.actor_id) if r.actor_id else None
        items.append({
            "id": r.id, "club_id": r.club_id, "event_type": r.event_type,
            "actor_id": r.actor_id, "actor_name": actor.full_name if actor else None,
            "data": r.data, "created_at": r.created_at,
        })
    return {"items": items, "page": page, "size": size}
