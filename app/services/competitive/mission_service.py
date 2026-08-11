from __future__ import annotations

"""
Mission Engine — Competitive Arena Phase 15 (LiveOps).

Architecture
------------
arena_missions is the admin-managed CATALOGUE/POOL. arena_player_missions
is the per-player ASSIGNED INSTANCE for a given period_key ('2026-08-10'
daily, ISO '2026-W32' weekly, '2026-08' monthly, the season/event UUID
string for seasonal/event missions) — assignment snapshots target_value so
an admin editing the catalogue mid-period never changes an already-handed-
out goal.

Assignment is BOTH lazy (assign_missions_for_user, called on first read of
a period a player hasn't been assigned yet — mirrors statistics_service.
get_or_create_statistics's lazy-row pattern) AND proactively batched by
the Celery beat daily/weekly/monthly reset tasks (app/workers/liveops_
tasks.py) for every user with an existing CompetitiveStatistics row — the
spec's literal "every player receives a new set of missions every day"
is passive, so the batch path is the primary one; the lazy path is only a
fallback for brand-new players the batch job hasn't reached yet.

record_event() is the SINGLE entry point every other service calls when
something mission-relevant happens (gameplay_service.finish_match,
battle_royale_service.update_battle_royale_statistics, replay_service,
tournament_service.register_for_tournament, login_service.checkin). It
updates every ACTIVE arena_player_missions row across every period whose
mission.metric_key matches, then (via event_service.record_event) feeds
any active Limited-Time/Community-Challenge/Educational-Campaign event
sharing that metric_key — one call site, every consumer.

metric_key registry actually wired to a real event source in this phase:
    arena_win                 gameplay_service.finish_match (result == win)
    arena_match_played        gameplay_service.finish_match (every participant)
    arena_question_answered   gameplay_service.finish_match (correct+wrong count)
    arena_accuracy_session    gameplay_service.finish_match (this match's accuracy_pct)
    arena_win_streak          gameplay_service.finish_match (mode=set_max, life_stats.current_streak)
    arena_perfect_match       gameplay_service.finish_match (accuracy_pct == 100)
    arena_club_battle_played  gameplay_service.finish_match (match_type == club_battle)
    arena_replay_watched      replay_service.record_view
    arena_tournament_joined   tournament_service.register_for_tournament
    arena_battle_royale_played / arena_br_win / arena_br_top10
                               battle_royale_service.update_battle_royale_statistics
    login_checkin              login_service.checkin
    arena_xp_earned            reward_service.grant_reward (reward_type == arena_xp)
    arena_league_family        arena_achievement_service-style state check (see _check_state_missions)
    arena_tournament_wins / arena_season_top1000 / arena_top100_rank
                               state-check metric_keys, re-evaluated by _check_state_missions
                               (called from the same hook points as above — cheap, small row counts)

Every other category the spec names (Learning/Homework/Lessons/Teachers/
Social) is fully catalogue-configurable (admin can create a mission with
any metric_key) but has NO wired event source yet in this phase — exactly
the same "reserved, not fabricated" posture as prior phases' unbacked
flags. Calling record_event with an unrecognized metric_key is a safe
no-op (no matching rows found).
"""

import logging
import random
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.models.admin import PlatformSettings
from app.models.competitive import CompetitiveStatistics
from app.models.liveops import ArenaMission, ArenaPlayerMission
from app.services.notification_engine import emit

logger = logging.getLogger(__name__)

ALGIERS_TZ = timezone(timedelta(hours=1))


def _settings(db: Session) -> PlatformSettings:
    return db.get(PlatformSettings, True) or PlatformSettings()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def period_key_for(period: str, *, season_id: Optional[UUID] = None, event_id: Optional[UUID] = None, at: Optional[datetime] = None) -> Optional[str]:
    at = at or _now()
    local = at.astimezone(ALGIERS_TZ)
    if period == "daily":
        return local.date().isoformat()
    if period == "weekly":
        iso = local.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    if period == "monthly":
        return f"{local.year:04d}-{local.month:02d}"
    if period == "seasonal":
        return str(season_id) if season_id else None
    if period == "event":
        return str(event_id) if event_id else None
    return None


def _period_end(period: str, *, at: Optional[datetime] = None) -> Optional[datetime]:
    at = at or _now()
    local = at.astimezone(ALGIERS_TZ)
    if period == "daily":
        end = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "weekly":
        days_left = 7 - local.isoweekday()
        end = (local + timedelta(days=days_left + 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "monthly":
        if local.month == 12:
            end = local.replace(year=local.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            end = local.replace(month=local.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        return None
    return end.astimezone(timezone.utc)


def pool_for_period(db: Session, period: str, *, season_id: Optional[UUID] = None, event_id: Optional[UUID] = None) -> List[ArenaMission]:
    query = select(ArenaMission).where(ArenaMission.period == period).where(ArenaMission.status == "published")
    if period == "seasonal":
        query = query.where((ArenaMission.season_id == season_id) | (ArenaMission.season_id.is_(None)))
    elif period == "event":
        query = query.where(ArenaMission.event_id == event_id)
    return list(db.exec(query.order_by(ArenaMission.sort_order.asc())).all())


def assign_missions_for_user(
    db: Session, user_id: UUID, period: str, *, season_id: Optional[UUID] = None, event_id: Optional[UUID] = None,
) -> List[ArenaPlayerMission]:
    period_key = period_key_for(period, season_id=season_id, event_id=event_id)
    if period_key is None:
        return []

    existing = list(db.exec(
        select(ArenaPlayerMission)
        .join(ArenaMission, ArenaMission.id == ArenaPlayerMission.mission_id)
        .where(ArenaPlayerMission.user_id == user_id)
        .where(ArenaPlayerMission.period_key == period_key)
        .where(ArenaMission.period == period)
    ).all())
    if existing:
        return existing

    pool = pool_for_period(db, period, season_id=season_id, event_id=event_id)
    if not pool:
        return []

    settings_row = _settings(db)
    if period == "event":
        chosen = pool  # every published objective for this event, not a random subset
    elif period == "seasonal":
        chosen = pool
    else:
        count = {
            "daily": settings_row.competitive_daily_missions_count,
            "weekly": settings_row.competitive_weekly_missions_count,
            "monthly": settings_row.competitive_monthly_missions_count,
        }.get(period, 3)
        chosen = random.sample(pool, k=min(count, len(pool)))

    expires_at = _period_end(period) if period in ("daily", "weekly", "monthly") else None
    assigned: List[ArenaPlayerMission] = []
    for mission in chosen:
        row = ArenaPlayerMission(
            user_id=user_id, mission_id=mission.id, period_key=period_key,
            target_value=mission.target_value, expires_at=expires_at,
        )
        db.add(row)
        assigned.append(row)
    try:
        db.commit()
    except Exception:
        db.rollback()  # a concurrent request already assigned this period — re-read below
        return list(db.exec(
            select(ArenaPlayerMission)
            .join(ArenaMission, ArenaMission.id == ArenaPlayerMission.mission_id)
            .where(ArenaPlayerMission.user_id == user_id)
            .where(ArenaPlayerMission.period_key == period_key)
            .where(ArenaMission.period == period)
        ).all())
    for row in assigned:
        db.refresh(row)

    if period == "daily" and assigned:
        emit(
            db, event_type="arena_new_daily_missions", user_id=user_id,
            dedup_key=f"arena_new_daily_missions:{user_id}:{period_key}",
        )
    return assigned


def list_missions_view(
    db: Session, user_id: UUID, period: str, *, season_id: Optional[UUID] = None, event_id: Optional[UUID] = None,
) -> List[Dict[str, Any]]:
    rows = assign_missions_for_user(db, user_id, period, season_id=season_id, event_id=event_id)
    if not rows:
        return []
    mission_ids = [r.mission_id for r in rows]
    missions = {m.id: m for m in db.exec(select(ArenaMission).where(ArenaMission.id.in_(mission_ids))).all()}

    out = []
    for row in rows:
        mission = missions.get(row.mission_id)
        if mission is None:
            continue
        remaining = max(0, row.target_value - row.progress_current)
        pct = round((row.progress_current / row.target_value) * 100, 1) if row.target_value else 100.0
        elapsed_hours = max((_now() - row.assigned_at).total_seconds() / 3600, 0.01)
        rate = row.progress_current / elapsed_hours
        eta_hours = round(remaining / rate, 1) if rate > 0 and row.status == "active" else None
        out.append({
            "id": row.id, "mission_id": mission.id, "code": mission.code,
            "title": mission.title, "description": mission.description, "icon": mission.icon,
            "category": mission.category, "mission_type": mission.mission_type, "period": mission.period,
            "difficulty": mission.difficulty, "reward_config": mission.reward_config,
            "progress_current": row.progress_current, "target_value": row.target_value,
            "progress_pct": min(100.0, pct), "remaining": remaining,
            "estimated_hours_remaining": eta_hours,
            "status": row.status, "assigned_at": row.assigned_at, "completed_at": row.completed_at,
            "claimed_at": row.claimed_at, "expires_at": row.expires_at,
            "rerolled_count": row.rerolled_count,
            "free_rerolls_max": mission.max_free_rerolls if mission.max_free_rerolls is not None else (
                _settings(db).competitive_mission_free_rerolls_daily if mission.period == "daily"
                else _settings(db).competitive_mission_free_rerolls_weekly if mission.period == "weekly" else 0
            ),
        })
    return out


def _in_time_window(time_window: Dict[str, Any], *, at: Optional[datetime] = None) -> bool:
    if not time_window:
        return True
    at = at or _now()
    local_hour = at.astimezone(ALGIERS_TZ).hour
    before = time_window.get("before_hour")
    after = time_window.get("after_hour")
    if before is not None and not (local_hour < before):
        return False
    if after is not None and not (local_hour >= after):
        return False
    return True


def record_event(db: Session, *, user_id: UUID, metric_key: str, amount: float = 1, mode: str = "increment") -> List[UUID]:
    """mode: 'increment' (counter/accumulation — adds amount), 'set_max'
    (streak — progress becomes max(current, amount)), or 'threshold'
    (boolean — amount is compared directly against target_value, e.g. this
    match's accuracy_pct against an 80/90/100 threshold mission).
    Returns the list of ArenaPlayerMission ids that were newly completed."""
    rows = list(db.exec(
        select(ArenaPlayerMission, ArenaMission)
        .join(ArenaMission, ArenaMission.id == ArenaPlayerMission.mission_id)
        .where(ArenaPlayerMission.user_id == user_id)
        .where(ArenaPlayerMission.status == "active")
        .where(ArenaMission.metric_key == metric_key)
    ).all())
    if not rows:
        try:
            from app.services.competitive import event_service
            event_service.record_event(db, user_id=user_id, metric_key=metric_key, amount=amount)
        except Exception:
            logger.debug("mission_service.record_event: event_service feed failed", exc_info=True)
        return []

    newly_completed: List[UUID] = []
    for player_mission, mission in rows:
        if mission.mission_type == "time_based" and not _in_time_window(mission.time_window):
            continue
        if mission.mission_type == "boolean":
            if amount >= player_mission.target_value:
                player_mission.progress_current = player_mission.target_value
        elif mode == "set_max" or mission.mission_type == "streak":
            player_mission.progress_current = max(player_mission.progress_current, int(amount))
        else:
            player_mission.progress_current = min(player_mission.target_value, player_mission.progress_current + int(amount))

        if player_mission.progress_current >= player_mission.target_value and player_mission.status == "active":
            player_mission.status = "completed"
            player_mission.completed_at = _now()
            newly_completed.append(player_mission.id)
        db.add(player_mission)
    db.commit()

    for pm_id in newly_completed:
        emit(
            db, event_type="arena_mission_completed", user_id=user_id,
            data={"player_mission_id": str(pm_id)},
            dedup_key=f"arena_mission_completed:{pm_id}",
        )
        from app.core import domain_events
        domain_events.publish(domain_events.MISSION_COMPLETED, user_id=user_id, player_mission_id=pm_id, metric_key=metric_key)

    try:
        from app.services.competitive import event_service
        event_service.record_event(db, user_id=user_id, metric_key=metric_key, amount=amount)
    except Exception:
        logger.debug("mission_service.record_event: event_service feed failed", exc_info=True)

    return newly_completed


def claim_mission(db: Session, user_id: UUID, player_mission_id: UUID) -> Dict[str, Any]:
    # Phase 16 — row-level lock: closes the race window between two
    # simultaneous claim requests both reading status='completed' before
    # either commits (the unique period/mission constraint alone doesn't
    # protect a re-claim of the SAME row — this does).
    row = db.exec(
        select(ArenaPlayerMission).where(ArenaPlayerMission.id == player_mission_id).with_for_update()
    ).first()
    if row is None or row.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mission introuvable.")
    if row.status == "claimed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Récompense déjà réclamée.")
    if row.status != "completed":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Mission non terminée.")

    stats = db.get(CompetitiveStatistics, user_id)
    if stats is not None and stats.suspended_until and stats.suspended_until > _now():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Compte Arène suspendu.")

    mission = db.get(ArenaMission, row.mission_id)

    # Phase 16 — claim-then-grant (see event_service.claim_event_reward's
    # identical comment): persist the idempotency flag before granting,
    # since grant_reward's internal commits release the row lock above.
    row.status = "claimed"
    row.claimed_at = _now()
    db.add(row)
    db.commit()

    from app.services.competitive import reward_service
    granted = []
    for entry in (mission.reward_config if mission else []) or []:
        reward_type = entry.get("reward_type")
        if not reward_type:
            continue
        grant = reward_service.grant_reward(
            db, user_id=user_id, season_id=None, source=f"mission:{mission.code}",
            reward_type=reward_type, reward_ref=entry.get("reward_ref"), reward_amount=entry.get("reward_amount"),
            notify=False,
        )
        granted.append({"reward_type": reward_type, "status": grant.status})

    return {"player_mission_id": row.id, "rewards": granted}


def reroll_mission(db: Session, user_id: UUID, player_mission_id: UUID) -> Dict[str, Any]:
    row = db.get(ArenaPlayerMission, player_mission_id)
    if row is None or row.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mission introuvable.")
    if row.status not in ("active",):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cette mission ne peut plus être relancée.")
    mission = db.get(ArenaMission, row.mission_id)
    if mission is None or mission.period not in ("daily", "weekly"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cette mission ne peut pas être relancée.")

    settings_row = _settings(db)
    max_rerolls = mission.max_free_rerolls if mission.max_free_rerolls is not None else (
        settings_row.competitive_mission_free_rerolls_daily if mission.period == "daily" else settings_row.competitive_mission_free_rerolls_weekly
    )
    if row.rerolled_count >= max_rerolls:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Nombre maximum de relances atteint.")

    already_assigned_ids = {
        r.mission_id for r in db.exec(
            select(ArenaPlayerMission).where(ArenaPlayerMission.user_id == user_id).where(ArenaPlayerMission.period_key == row.period_key)
        ).all()
    }
    pool = [m for m in pool_for_period(db, mission.period) if m.id not in already_assigned_ids]
    if not pool:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Aucune autre mission disponible.")

    new_mission = random.choice(pool)
    row.mission_id = new_mission.id
    row.target_value = new_mission.target_value
    row.progress_current = 0
    row.status = "active"
    row.completed_at = None
    row.claimed_at = None
    row.rerolled_count += 1
    row.assigned_at = _now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"player_mission_id": row.id, "mission_id": new_mission.id, "title": new_mission.title}


# ─── Batch reset (Celery beat) ──────────────────────────────────────────────

def reset_period_for_all(db: Session, period: str) -> int:
    """Proactively assigns the new period's missions to every user who has
    ever played the Arena (has a competitive_statistics row) — the primary
    path satisfying the spec's literal 'every player receives a new set of
    missions every day' (passive, no page visit required). assign_missions_
    for_user is idempotent (unique constraint) so re-running this is safe."""
    user_ids = list(db.exec(select(CompetitiveStatistics.user_id)).all())
    assigned = 0
    for user_id in user_ids:
        rows = assign_missions_for_user(db, user_id, period)
        if rows:
            assigned += 1
    logger.info("mission_service.reset_period_for_all: period=%s users_assigned=%s", period, assigned)
    return assigned


def notify_missions_almost_done(db: Session) -> int:
    settings_row = _settings(db)
    threshold_pct = settings_row.competitive_mission_almost_done_pct
    rows = db.exec(
        select(ArenaPlayerMission).where(ArenaPlayerMission.status == "active").where(ArenaPlayerMission.target_value > 0)
    ).all()
    sent = 0
    for row in rows:
        pct = (row.progress_current / row.target_value) * 100
        if pct < threshold_pct:
            continue
        result = emit(
            db, event_type="arena_mission_almost_done", user_id=row.user_id,
            data={"player_mission_id": str(row.id)},
            dedup_key=f"arena_mission_almost_done:{row.id}",
        )
        if result is not None:
            sent += 1
    return sent


def notify_missions_expire_tonight(db: Session) -> int:
    """Runs once in the evening (see beat_schedule) — daily missions whose
    expires_at falls within the next few hours."""
    now = _now()
    window_end = now + timedelta(hours=4)
    rows = db.exec(
        select(ArenaPlayerMission)
        .where(ArenaPlayerMission.status == "active")
        .where(ArenaPlayerMission.expires_at.is_not(None))
        .where(ArenaPlayerMission.expires_at <= window_end)
        .where(ArenaPlayerMission.expires_at > now)
    ).all()
    sent = 0
    for row in rows:
        result = emit(
            db, event_type="arena_mission_expires_tonight", user_id=row.user_id,
            data={"player_mission_id": str(row.id)},
            dedup_key=f"arena_mission_expires_tonight:{row.id}:{row.period_key}",
        )
        if result is not None:
            sent += 1
    return sent


# ─── State-check missions (mirrors arena_achievement_service's pattern) ────

def check_state_missions(db: Session, user_id: UUID) -> None:
    """Some mission metric_keys aren't incremented by an event — they're a
    current-state check (e.g. 'reach Platinum league', 'finish Top 1000') —
    re-evaluated opportunistically from the same call sites as record_event,
    exactly like arena_achievement_service's league/rank condition_types."""
    stats = db.get(CompetitiveStatistics, user_id)
    if stats is None:
        return

    from app.models.competitive import RANK_TIERS
    try:
        family_index = RANK_TIERS.index(stats.rank_tier) + 1
    except ValueError:
        family_index = 0
    record_event(db, user_id=user_id, metric_key="arena_league_family", amount=family_index, mode="set_max")
    record_event(db, user_id=user_id, metric_key="arena_tournament_wins", amount=stats.tournament_wins, mode="set_max")

    from app.models.competitive import CompetitiveSeasonStats
    season_row = db.exec(
        select(CompetitiveSeasonStats).where(CompetitiveSeasonStats.user_id == user_id).order_by(CompetitiveSeasonStats.updated_at.desc())
    ).first()
    if season_row is not None:
        global_rank = None
        try:
            from app.services.competitive import leaderboard_service
            global_rank = leaderboard_service.get_global_rank(db, user_id)
        except Exception:
            pass
        if global_rank is not None:
            if global_rank <= 1000:
                record_event(db, user_id=user_id, metric_key="arena_season_top1000", amount=1)
            if global_rank <= 100:
                record_event(db, user_id=user_id, metric_key="arena_top100_rank", amount=1)
