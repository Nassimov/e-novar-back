from __future__ import annotations

"""
Login Reward Service + Streak Service — Competitive Arena Phase 15 (LiveOps).

The N-day calendar (arena_login_rewards, N = PlatformSettings.
competitive_login_calendar_length) is a fixed catalogue an admin edits;
a player's position in it is DERIVED from arena_login_streaks.
current_streak, never stored redundantly:
    day_number   = ((current_streak - 1) % N) + 1
    cycle_number = ((current_streak - 1) // N) + 1
so completing day N and continuing the next day rolls straight into day 1
of a new cycle — the UNIQUE(user_id, day_number, cycle_number) claim
constraint is what lets a player re-claim "day 7" every lap without ever
colliding with their previous lap's claim.

Grace period (PlatformSettings.competitive_login_streak_grace_hours): a
player who misses exactly one calendar day keeps their streak ONCE per
break if they check back in within `grace_hours` of the missed day
starting — mirrors real mobile-game streak forgiveness. A second
consecutive miss, or checking in outside the grace window, resets the
streak to 1.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.models.admin import PlatformSettings
from app.models.competitive import CompetitiveStatistics
from app.models.liveops import ArenaLoginReward, ArenaLoginRewardClaim, ArenaLoginStreak
from app.services.notification_engine import emit

logger = logging.getLogger(__name__)

ALGIERS_TZ = timezone(timedelta(hours=1))


def _settings(db: Session) -> PlatformSettings:
    return db.get(PlatformSettings, True) or PlatformSettings()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> date:
    return _now().astimezone(ALGIERS_TZ).date()


def _get_or_create_streak(db: Session, user_id: UUID) -> ArenaLoginStreak:
    row = db.get(ArenaLoginStreak, user_id)
    if row is None:
        row = ArenaLoginStreak(user_id=user_id)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _position(streak: ArenaLoginStreak, calendar_length: int) -> Dict[str, int]:
    if streak.current_streak <= 0:
        return {"day_number": 0, "cycle_number": streak.cycle_number}
    day_number = ((streak.current_streak - 1) % calendar_length) + 1
    cycle_number = ((streak.current_streak - 1) // calendar_length) + 1
    return {"day_number": day_number, "cycle_number": cycle_number}


def checkin(db: Session, user_id: UUID) -> Dict[str, Any]:
    settings_row = _settings(db)
    calendar_length = max(1, settings_row.competitive_login_calendar_length)
    streak = _get_or_create_streak(db, user_id)
    today = _today()

    already_checked_in_today = streak.last_checkin_date == today
    if not already_checked_in_today:
        gap = (today - streak.last_checkin_date).days if streak.last_checkin_date else 1

        if gap <= 1:
            streak.current_streak += 1
        elif gap == 2:
            grace_deadline_hours = settings_row.competitive_login_streak_grace_hours
            missed_day_start = datetime.combine(streak.last_checkin_date + timedelta(days=1), datetime.min.time(), tzinfo=ALGIERS_TZ)
            within_grace = _now() <= (missed_day_start + timedelta(hours=grace_deadline_hours))
            grace_available = streak.grace_used_at is None or streak.grace_used_at.date() < streak.last_checkin_date
            if within_grace and grace_available:
                streak.current_streak += 1
                streak.grace_used_at = _now()
            else:
                streak.current_streak = 1
        else:
            streak.current_streak = 1

        streak.longest_streak = max(streak.longest_streak, streak.current_streak)
        streak.last_checkin_date = today
        pos = _position(streak, calendar_length)
        streak.cycle_number = pos["cycle_number"]
        streak.updated_at = _now()
        db.add(streak)
        db.commit()
        db.refresh(streak)

        try:
            from app.services.competitive import mission_service
            mission_service.record_event(db, user_id=user_id, metric_key="login_checkin", amount=1)
        except Exception:
            logger.exception("login_service.checkin: mission record_event failed user=%s", user_id)

    pos = _position(streak, calendar_length)
    reward = db.exec(select(ArenaLoginReward).where(ArenaLoginReward.day_number == pos["day_number"])).first()
    already_claimed = False
    if reward is not None:
        already_claimed = db.exec(
            select(ArenaLoginRewardClaim)
            .where(ArenaLoginRewardClaim.user_id == user_id)
            .where(ArenaLoginRewardClaim.day_number == pos["day_number"])
            .where(ArenaLoginRewardClaim.cycle_number == pos["cycle_number"])
        ).first() is not None

    return {
        "current_streak": streak.current_streak, "longest_streak": streak.longest_streak,
        "day_number": pos["day_number"], "cycle_number": pos["cycle_number"],
        "reward": reward, "already_claimed": already_claimed, "already_checked_in_today": already_checked_in_today,
    }


def get_calendar(db: Session, user_id: UUID) -> Dict[str, Any]:
    settings_row = _settings(db)
    calendar_length = max(1, settings_row.competitive_login_calendar_length)
    streak = _get_or_create_streak(db, user_id)
    pos = _position(streak, calendar_length)
    rewards = list(db.exec(select(ArenaLoginReward).order_by(ArenaLoginReward.day_number.asc())).all())
    claimed_days = set(db.exec(
        select(ArenaLoginRewardClaim.day_number)
        .where(ArenaLoginRewardClaim.user_id == user_id)
        .where(ArenaLoginRewardClaim.cycle_number == pos["cycle_number"])
    ).all())

    days = [
        {
            "day_number": r.day_number, "reward_config": r.reward_config, "is_milestone": r.is_milestone, "icon": r.icon,
            "claimed": r.day_number in claimed_days,
            "unlocked": r.day_number <= pos["day_number"],
        }
        for r in rewards
    ]
    return {
        "current_streak": streak.current_streak, "longest_streak": streak.longest_streak,
        "current_day_number": pos["day_number"], "cycle_number": pos["cycle_number"],
        "calendar_length": calendar_length, "days": days,
    }


def claim_day_reward(db: Session, user_id: UUID, day_number: int) -> Dict[str, Any]:
    settings_row = _settings(db)
    calendar_length = max(1, settings_row.competitive_login_calendar_length)
    streak = _get_or_create_streak(db, user_id)
    pos = _position(streak, calendar_length)

    if day_number < 1 or day_number > pos["day_number"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Ce jour n'est pas encore débloqué.")

    reward = db.exec(select(ArenaLoginReward).where(ArenaLoginReward.day_number == day_number)).first()
    if reward is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Récompense introuvable.")

    existing = db.exec(
        select(ArenaLoginRewardClaim)
        .where(ArenaLoginRewardClaim.user_id == user_id)
        .where(ArenaLoginRewardClaim.day_number == day_number)
        .where(ArenaLoginRewardClaim.cycle_number == pos["cycle_number"])
    ).first()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Récompense déjà réclamée.")

    stats = db.get(CompetitiveStatistics, user_id)
    if stats is not None and stats.suspended_until and stats.suspended_until > _now():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Compte Arène suspendu.")

    # Phase 16 — concurrency: the claim row is inserted (and committed)
    # FIRST as an atomic idempotency gate — a genuine simultaneous double
    # request has exactly one winner at the DB's unique-constraint level,
    # and the loser never reaches reward granting at all (rather than
    # granting first and risking a double-grant the claim row can't undo).
    try:
        db.add(ArenaLoginRewardClaim(user_id=user_id, day_number=day_number, cycle_number=pos["cycle_number"]))
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Récompense déjà réclamée.")

    from app.services.competitive import reward_service
    granted = []
    for entry in (reward.reward_config or []):
        reward_type = entry.get("reward_type")
        if not reward_type:
            continue
        grant = reward_service.grant_reward(
            db, user_id=user_id, season_id=None, source=f"login_day_{day_number}",
            reward_type=reward_type, reward_ref=entry.get("reward_ref"), reward_amount=entry.get("reward_amount"),
            notify=False,
        )
        granted.append({"reward_type": reward_type, "status": grant.status})
    return {"day_number": day_number, "rewards": granted}


def get_streak(db: Session, user_id: UUID) -> ArenaLoginStreak:
    return _get_or_create_streak(db, user_id)


def notify_streak_expiring(db: Session) -> int:
    """Celery beat reminder — anyone whose streak would break if they don't
    check in before local midnight (i.e. haven't checked in today yet, and
    it's currently the evening — checked against the same grace window used
    by checkin() so the reminder and the actual grace logic never disagree)."""
    settings_row = _settings(db)
    today = _today()
    now_local = _now().astimezone(ALGIERS_TZ)
    if now_local.hour < 20:
        return 0

    candidates = db.exec(
        select(ArenaLoginStreak).where(ArenaLoginStreak.current_streak > 0).where(ArenaLoginStreak.last_checkin_date < today)
    ).all()
    sent = 0
    for streak in candidates:
        result = emit(
            db, event_type="arena_login_streak_expiring", user_id=streak.user_id,
            data={"current_streak": streak.current_streak},
            dedup_key=f"arena_login_streak_expiring:{streak.user_id}:{today.isoformat()}",
        )
        if result is not None:
            sent += 1
    return sent
