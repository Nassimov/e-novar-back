from __future__ import annotations

"""
Club Reputation Engine — Competitive Arena Phase 11, Part B.

A single funnel for every reputation change (clubs.reputation is a running
counter, migration 089; club_reputation_events is its audit ledger,
migration 090) — mirrors reward_service.grant_reward's role as the one place
that mutates a currency-like counter and records why. Gain amounts are
admin-configurable (PlatformSettings.competitive_club_reputation_*), never
hardcoded, per spec's "Administrator configurable".

Reputation never goes negative (floored at 0) — it's a standing/trust score,
not a currency that can be "spent into debt".
"""

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlmodel import Session

from app.models.admin import PlatformSettings
from app.models.club import Club, ClubReputationEvent

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _settings(db: Session) -> PlatformSettings:
    return db.get(PlatformSettings, True) or PlatformSettings()


def award_reputation(db: Session, club: Club, *, delta: int, reason: str, actor_id: Optional[UUID] = None) -> Club:
    if delta == 0:
        return club
    club.reputation = max(0, (club.reputation or 0) + delta)
    db.add(club)
    db.add(ClubReputationEvent(club_id=club.id, delta=delta, reason=reason, actor_id=actor_id))
    db.commit()
    db.refresh(club)
    return club


def award_battle_win_reputation(db: Session, club: Club) -> Club:
    settings_row = _settings(db)
    return award_reputation(db, club, delta=settings_row.competitive_club_reputation_battle_win, reason="battle_win")


def award_daily_activity_reputation(db: Session, club: Club) -> Club:
    settings_row = _settings(db)
    return award_reputation(db, club, delta=settings_row.competitive_club_reputation_activity_daily, reason="daily_activity")


def award_achievement_reputation(db: Session, club: Club) -> Club:
    settings_row = _settings(db)
    return award_reputation(db, club, delta=settings_row.competitive_club_reputation_achievement, reason="achievement_unlocked")


def penalize_abuse_report(db: Session, club: Club, *, actor_id: Optional[UUID] = None) -> Club:
    settings_row = _settings(db)
    return award_reputation(db, club, delta=settings_row.competitive_club_reputation_abuse_report, reason="abuse_report", actor_id=actor_id)
