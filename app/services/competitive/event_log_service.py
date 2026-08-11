from __future__ import annotations

"""
Match Event Log — Competitive Arena Phase 2.

Append-only audit trail for a single match (invitation lifecycle, ready
toggles, disconnects, admin actions) — see competitive_match_events in
migration 075. Never raises: logging a match's history must never break the
action that triggered it (same defensive posture as notification_engine.emit).
"""

import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlmodel import Session, select

from app.models.competitive import CompetitiveMatchEvent

logger = logging.getLogger(__name__)


def log_event(
    db: Session,
    *,
    match_id: UUID,
    actor_id: Optional[UUID],
    event_type: str,
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    try:
        db.add(CompetitiveMatchEvent(match_id=match_id, actor_id=actor_id, event_type=event_type, meta=meta or {}))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.exception("log_event failed match_id=%s event_type=%s", match_id, event_type)


def list_events(db: Session, match_id: UUID) -> List[CompetitiveMatchEvent]:
    return list(
        db.exec(
            select(CompetitiveMatchEvent)
            .where(CompetitiveMatchEvent.match_id == match_id)
            .order_by(CompetitiveMatchEvent.created_at.desc())
        ).all()
    )
