from __future__ import annotations

"""
Connection Service — Competitive Arena Phase 5.

Durable connection/latency audit trail (competitive_match_connections) on
top of Phase 2's Redis-backed hot presence state (presence_service.py) —
Redis stays the fast "is this user online right now" path; this table is
the persisted record for analytics/moderation/fraud-detection, per the
spec's "Event Log ... used later for ... Fraud detection, Analytics."
Never allowed to break the WS connection lifecycle it's attached to.
"""

import logging
from typing import Optional
from uuid import UUID

from sqlmodel import Session

from app.models.competitive import CompetitiveMatchConnection

logger = logging.getLogger(__name__)


def log_connection_event(
    db: Session, *, match_id: UUID, user_id: UUID, event_type: str, latency_ms: Optional[int] = None,
) -> None:
    try:
        db.add(CompetitiveMatchConnection(match_id=match_id, user_id=user_id, event_type=event_type, latency_ms=latency_ms))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.exception("log_connection_event failed match=%s user=%s event=%s", match_id, user_id, event_type)
