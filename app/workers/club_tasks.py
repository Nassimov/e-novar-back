from __future__ import annotations

"""
Clash Club — background tasks (Phase 11, Part B/C).

Note: gameplay TICKING for a club battle needs no dedicated task —
task_gameplay_tick (competitive_tasks.py) is already match_type-agnostic
and self-schedules per-match (see gameplay_service.schedule_next_tick).
Likewise, club battle reminders reuse the EXISTING
task_send_competitive_match_reminders (it scans competitive_matches.
scheduled_at across every match_type generically) — no club-specific
reminder task exists here.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task
def task_expire_club_battle_challenges() -> Dict[str, int]:
    from sqlmodel import Session, select

    from app.database import get_engine
    from app.models.club import ClubBattleChallenge

    now = datetime.now(timezone.utc)
    engine = get_engine()
    expired = 0
    with Session(engine) as db:
        stale = db.exec(
            select(ClubBattleChallenge)
            .where(ClubBattleChallenge.status == "pending")
            .where(ClubBattleChallenge.expires_at.is_not(None))
            .where(ClubBattleChallenge.expires_at <= now)
        ).all()
        for challenge in stale:
            challenge.status = "expired"
            challenge.responded_at = now
            db.add(challenge)
            expired += 1
        db.commit()
    logger.info("task_expire_club_battle_challenges: expired=%s", expired)
    return {"expired": expired}


@celery_app.task
def task_check_club_season_end() -> Dict[str, Any]:
    from sqlmodel import Session

    from app.database import get_engine
    from app.services.club import season_service

    engine = get_engine()
    with Session(engine) as db:
        ended_season_id = season_service.check_and_trigger_season_end(db)
    if ended_season_id is not None:
        logger.info("task_check_club_season_end: ended season=%s", ended_season_id)
    return {"ended_season_id": str(ended_season_id) if ended_season_id else None}


@celery_app.task
def task_rollup_club_daily_activity() -> Dict[str, int]:
    """Nightly — populates yesterday's public.club_daily_activity row for
    every club that had any chat/battle/join activity, so
    analytics_service can answer 'daily/weekly activity'/'retention' in
    O(days) at read time (see migration 090's header)."""
    from sqlmodel import Session, func, select

    from app.database import get_engine
    from app.models.club import Club, ClubChatMessage, ClubDailyActivity, ClubMember
    from app.models.competitive import CompetitiveMatch, CompetitiveMatchParticipant

    yesterday = date.today() - timedelta(days=1)
    day_start = datetime.combine(yesterday, datetime.min.time(), tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    engine = get_engine()
    rows_written = 0
    with Session(engine) as db:
        club_ids = list(db.exec(select(Club.id).where(Club.status == "active")).all())
        for club_id in club_ids:
            message_count = db.exec(
                select(func.count()).select_from(
                    select(ClubChatMessage.id).where(ClubChatMessage.club_id == club_id)
                    .where(ClubChatMessage.created_at >= day_start).where(ClubChatMessage.created_at < day_end).subquery()
                )
            ).one()
            active_authors = db.exec(
                select(func.count(func.distinct(ClubChatMessage.user_id))).where(ClubChatMessage.club_id == club_id)
                .where(ClubChatMessage.created_at >= day_start).where(ClubChatMessage.created_at < day_end)
            ).one()
            new_members = db.exec(
                select(func.count()).select_from(
                    select(ClubMember.id).where(ClubMember.club_id == club_id)
                    .where(ClubMember.joined_at >= day_start).where(ClubMember.joined_at < day_end).subquery()
                )
            ).one()
            battles_played = db.exec(
                select(func.count(func.distinct(CompetitiveMatchParticipant.match_id))).select_from(CompetitiveMatchParticipant)
                .join(CompetitiveMatch, CompetitiveMatch.id == CompetitiveMatchParticipant.match_id)
                .where(CompetitiveMatchParticipant.club_id == club_id)
                .where(CompetitiveMatch.completed_at >= day_start).where(CompetitiveMatch.completed_at < day_end)
            ).one()

            if not (message_count or new_members or battles_played):
                continue

            existing = db.exec(
                select(ClubDailyActivity).where(ClubDailyActivity.club_id == club_id).where(ClubDailyActivity.activity_date == yesterday)
            ).first()
            if existing is None:
                existing = ClubDailyActivity(club_id=club_id, activity_date=yesterday)
            existing.message_count = message_count or 0
            existing.active_member_count = active_authors or 0
            existing.new_members = new_members or 0
            existing.battles_played = battles_played or 0
            db.add(existing)
            rows_written += 1
        db.commit()

    logger.info("task_rollup_club_daily_activity: date=%s rows_written=%s", yesterday, rows_written)
    return {"rows_written": rows_written}
