from __future__ import annotations

"""
Queue Event Log — Competitive Arena Phase 6.

Append-only audit trail for pre-match queue activity (join/leave/expansion/
matched/accepted/declined/timeout) — mirrors event_log_service.py's
CompetitiveMatchEvent convention, scoped to activity that happens before a
real match_id exists. Never raises: logging must never break the queue
action that triggered it.
"""

import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlmodel import Session, select

from app.models.competitive import CompetitiveQueueEvent

logger = logging.getLogger(__name__)


def log_event(
    db: Session,
    *,
    user_id: UUID,
    queue_entry_id: Optional[UUID],
    event_type: str,
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    try:
        db.add(CompetitiveQueueEvent(
            user_id=user_id, queue_entry_id=queue_entry_id, event_type=event_type, meta=meta or {},
        ))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.exception("log_event failed user_id=%s event_type=%s", user_id, event_type)


def list_events(db: Session, queue_entry_id: UUID) -> List[CompetitiveQueueEvent]:
    return list(
        db.exec(
            select(CompetitiveQueueEvent)
            .where(CompetitiveQueueEvent.queue_entry_id == queue_entry_id)
            .order_by(CompetitiveQueueEvent.created_at.desc())
        ).all()
    )


def recent_events(db: Session, *, event_type: Optional[str] = None, since=None) -> List[CompetitiveQueueEvent]:
    """Used by the admin analytics endpoint to compute live counters
    (matches created / cancelled / expired in the last hour, etc.) without a
    separate pre-aggregated statistics table."""
    query = select(CompetitiveQueueEvent)
    if event_type:
        query = query.where(CompetitiveQueueEvent.event_type == event_type)
    if since is not None:
        query = query.where(CompetitiveQueueEvent.created_at >= since)
    return list(db.exec(query.order_by(CompetitiveQueueEvent.created_at.desc())).all())
