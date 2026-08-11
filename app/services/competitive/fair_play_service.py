from __future__ import annotations

"""
Fair Play Service — Competitive Arena Phase 13.

A single funnel for every Fair Play score change (CompetitiveStatistics.
fair_play_score is the running counter, starts at 100; competitive_fair_
play_events is its audit ledger) — mirrors club/reputation_service.py's
exact same "one function mutates the counter and records why" shape.
Displayed internally only (spec: "Displayed internally. Future public
badge possible.") — no frontend surface renders this to other players in
V1, only the owning student's own profile (see stats router) and the admin
panel.

Clamped to [0, 100] — a trust/standing score, not a currency that can be
spent into debt or inflated without bound.
"""

import logging
from typing import Optional
from uuid import UUID

from sqlmodel import Session

from app.models.admin import PlatformSettings
from app.models.competitive import CompetitiveFairPlayEvent, CompetitiveStatistics

logger = logging.getLogger(__name__)

_MIN_SCORE = 0
_MAX_SCORE = 100


def _settings(db: Session) -> PlatformSettings:
    return db.get(PlatformSettings, True) or PlatformSettings()


def award(db: Session, user_id: UUID, *, delta: int, reason: str, match_id: Optional[UUID] = None) -> None:
    """Best-effort — a fair play update must never break the gameplay/
    moderation action that triggered it."""
    if delta == 0:
        return
    try:
        stats = db.get(CompetitiveStatistics, user_id)
        if stats is None:
            from app.services.competitive import statistics_service
            stats = statistics_service.get_or_create_statistics(db, user_id)
        stats.fair_play_score = max(_MIN_SCORE, min(_MAX_SCORE, stats.fair_play_score + delta))
        db.add(stats)
        db.add(CompetitiveFairPlayEvent(user_id=user_id, match_id=match_id, delta=delta, reason=reason))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.exception("fair_play_service.award failed user=%s reason=%s", user_id, reason)


def penalize_disconnect(db: Session, user_id: UUID, *, match_id: Optional[UUID] = None) -> None:
    settings_row = _settings(db)
    award(db, user_id, delta=settings_row.competitive_fair_play_disconnect_penalty, reason="disconnect", match_id=match_id)


def penalize_confirmed_report(db: Session, user_id: UUID, *, match_id: Optional[UUID] = None) -> None:
    settings_row = _settings(db)
    award(db, user_id, delta=settings_row.competitive_fair_play_report_penalty, reason="confirmed_report", match_id=match_id)


def penalize_afk(db: Session, user_id: UUID, *, match_id: Optional[UUID] = None) -> None:
    settings_row = _settings(db)
    award(db, user_id, delta=settings_row.competitive_fair_play_afk_penalty, reason="afk", match_id=match_id)


def reward_clean_match(db: Session, user_id: UUID, *, match_id: Optional[UUID] = None) -> None:
    settings_row = _settings(db)
    award(db, user_id, delta=settings_row.competitive_fair_play_clean_match_bonus, reason="clean_match", match_id=match_id)


def is_eligible_for_ranked(db: Session, stats: CompetitiveStatistics) -> bool:
    settings_row = _settings(db)
    return stats.fair_play_score >= settings_row.competitive_fair_play_min_for_ranked
