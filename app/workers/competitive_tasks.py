from __future__ import annotations

"""
Competitive Arena — Phase 1 background tasks.

Expires stale pending invitations (and returns their match to 'draft' so
the creator can invite someone else) and stale draft matches that were
never followed by an invitation.
"""

import logging
from datetime import datetime, timezone
from typing import Dict

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task
def task_expire_competitive_invitations() -> Dict[str, int]:
    from sqlmodel import Session, select

    from app.database import get_engine
    from app.models.competitive import CompetitiveInvitation, CompetitiveMatch
    from app.services.competitive import match_service

    expired_invitations = 0
    expired_matches = 0
    now = datetime.now(timezone.utc)
    engine = get_engine()

    with Session(engine) as db:
        stale_invitations = db.exec(
            select(CompetitiveInvitation)
            .where(CompetitiveInvitation.status == "pending")
            .where(CompetitiveInvitation.expires_at <= now)
        ).all()
        for invitation in stale_invitations:
            invitation.status = "expired"
            invitation.responded_at = now
            db.add(invitation)
            match = db.get(CompetitiveMatch, invitation.match_id)
            if match is not None and match.status == "waiting_for_opponent":
                match_service.transition(match, "draft")
                db.add(match)
            expired_invitations += 1
        db.commit()

        stale_matches = db.exec(
            select(CompetitiveMatch)
            .where(CompetitiveMatch.status == "draft")
            .where(CompetitiveMatch.expires_at.is_not(None))
            .where(CompetitiveMatch.expires_at <= now)
        ).all()
        for match in stale_matches:
            match_service.transition(match, "expired")
            db.add(match)
            expired_matches += 1
        db.commit()

    logger.info(
        "task_expire_competitive_invitations: expired_invitations=%s expired_matches=%s",
        expired_invitations, expired_matches,
    )
    return {"expired_invitations": expired_invitations, "expired_matches": expired_matches}
