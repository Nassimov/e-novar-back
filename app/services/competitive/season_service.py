from __future__ import annotations

"""
Season Manager — Competitive Arena Phase 7.

Only one season may ever have status='active' (DB partial unique index on
migration 082). Season rows and their competitive_season_stats are NEVER
deleted — "history must never be lost" — a season is only ever frozen
(status='ended').

record_match_for_season() is the O(1)-per-match update the spec calls for
("avoid recalculating the entire leaderboard after every match — only
update impacted players"): it touches exactly the two participants'
season_stats rows, nothing else.

end_season() is the full "Season End Workflow": freeze rankings (compute
final_rank for every participant), distribute configured rewards
(reward_service), reset ratings per the season's configured strategy, and
auto-activate the next 'upcoming' season if the admin already queued one.
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.models.admin import PlatformSettings
from app.models.competitive import CompetitiveLeague, CompetitiveSeason, CompetitiveSeasonStats, CompetitiveStatistics
from app.services.competitive import ranking_service, reward_service
from app.services.notification_engine import emit

logger = logging.getLogger(__name__)


def _settings(db: Session) -> PlatformSettings:
    return db.get(PlatformSettings, True) or PlatformSettings()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get_active_season(db: Session) -> Optional[CompetitiveSeason]:
    return db.exec(select(CompetitiveSeason).where(CompetitiveSeason.status == "active")).first()


def get_season_or_404(db: Session, season_id: UUID) -> CompetitiveSeason:
    season = db.get(CompetitiveSeason, season_id)
    if season is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Saison introuvable.")
    return season


def list_seasons(db: Session) -> List[CompetitiveSeason]:
    return list(db.exec(select(CompetitiveSeason).order_by(CompetitiveSeason.starts_at.desc())).all())


# ─── Admin CRUD ──────────────────────────────────────────────────────────────

def create_season(
    db: Session, *, name: str, description: Optional[str], starts_at: datetime, ends_at: datetime,
    reset_strategy: str, reset_percentage: Optional[int], rules: dict, activate_now: bool,
) -> CompetitiveSeason:
    if reset_strategy not in ("soft", "hard", "percentage"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="reset_strategy invalide.")
    if ends_at <= starts_at:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="ends_at doit être après starts_at.")

    season = CompetitiveSeason(
        name=name, description=description, starts_at=starts_at, ends_at=ends_at,
        reset_strategy=reset_strategy, reset_percentage=reset_percentage, rules=rules or {},
        status="upcoming",
    )
    db.add(season)
    db.commit()
    db.refresh(season)

    if activate_now:
        season = activate_season(db, season.id)
    logger.info("competitive season created: id=%s name=%s activate_now=%s", season.id, season.name, activate_now)
    return season


def update_season(db: Session, season_id: UUID, **fields) -> CompetitiveSeason:
    season = get_season_or_404(db, season_id)
    if season.status == "ended":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Impossible de modifier une saison terminée.")
    for key, value in fields.items():
        if value is not None and hasattr(season, key):
            setattr(season, key, value)
    season.updated_at = _now()
    db.add(season)
    db.commit()
    db.refresh(season)
    return season


def activate_season(db: Session, season_id: UUID) -> CompetitiveSeason:
    """Ends whatever season is currently active first (Season End Workflow),
    then activates the target — the DB's partial unique index would reject
    two simultaneously-active rows anyway, but doing it explicitly runs the
    real end-of-season workflow instead of silently orphaning the old one."""
    target = get_season_or_404(db, season_id)
    if target.status == "ended":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cette saison est déjà terminée.")

    current = get_active_season(db)
    if current is not None and current.id != target.id:
        end_season(db, current.id, auto_activate_next=False)
        target = get_season_or_404(db, season_id)  # refresh after possible flush

    target.status = "active"
    target.updated_at = _now()
    db.add(target)
    db.commit()
    db.refresh(target)

    _notify_season_started(db, target)
    logger.info("competitive season activated: id=%s name=%s", target.id, target.name)
    return target


def _notify_season_started(db: Session, season: CompetitiveSeason) -> None:
    user_ids = list(db.exec(select(CompetitiveStatistics.user_id)).all())
    for user_id in user_ids:
        emit(
            db, event_type="competitive_new_season_started", user_id=user_id,
            data={"season_id": str(season.id), "season_name": season.name},
            dedup_key=f"competitive_new_season_started:{season.id}:{user_id}",
        )


# ─── Per-match bookkeeping ───────────────────────────────────────────────────

def record_match_for_season(
    db: Session, *, season_id: UUID, user_id: UUID, rating_after: int, league_id: Optional[UUID],
    result: str, accuracy_pct: float, response_time_ms: Optional[int],
) -> CompetitiveSeasonStats:
    row = db.exec(
        select(CompetitiveSeasonStats)
        .where(CompetitiveSeasonStats.season_id == season_id)
        .where(CompetitiveSeasonStats.user_id == user_id)
    ).first()
    if row is None:
        row = CompetitiveSeasonStats(season_id=season_id, user_id=user_id, mmr=rating_after, league_id=league_id)

    prev_total = row.matches_played
    row.matches_played += 1
    if result == "win":
        row.wins += 1
    elif result == "loss":
        row.losses += 1
    else:
        row.draws += 1
    row.mmr = rating_after
    row.league_id = league_id
    row.accuracy_pct = round(((row.accuracy_pct * prev_total) + accuracy_pct) / row.matches_played, 2)
    if response_time_ms is not None:
        row.avg_response_time_ms = (
            response_time_ms if row.avg_response_time_ms is None
            else round(((row.avg_response_time_ms * prev_total) + response_time_ms) / row.matches_played)
        )
    row.updated_at = _now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ─── Season End Workflow ─────────────────────────────────────────────────────

def _reset_rating(rating: int, *, strategy: str, percentage: int, initial: int, floor: int) -> int:
    if strategy == "hard":
        return initial
    pct = 100 if strategy == "hard" else percentage
    new_rating = round(initial + (rating - initial) * (1 - pct / 100))
    return max(floor, new_rating)


def end_season(db: Session, season_id: UUID, *, auto_activate_next: bool = True) -> CompetitiveSeason:
    season = get_season_or_404(db, season_id)
    if season.status == "ended":
        return season
    settings_row = _settings(db)

    # 1. Freeze rankings — compute final_rank for every participant, ordered
    #    by mmr desc (ties broken by lower matches_played = higher efficiency,
    #    then user_id for full determinism).
    rows = list(db.exec(
        select(CompetitiveSeasonStats)
        .where(CompetitiveSeasonStats.season_id == season_id)
        .order_by(CompetitiveSeasonStats.mmr.desc(), CompetitiveSeasonStats.matches_played.asc())
    ).all())
    for idx, row in enumerate(rows, start=1):
        row.final_rank = idx
        db.add(row)
    db.commit()

    # Phase 14 — Arena Achievement Engine: final_rank is now frozen, so
    # 'arena_season_final_rank' (Top 100/Top 10/Champion) achievements can
    # evaluate honestly. 'arena_league_reached' is re-checked too (a
    # player's best_rank_tier only ever moves during the season itself, but
    # checking here as well costs nothing and catches anyone who was never
    # re-evaluated mid-season for any reason).
    from app.services.competitive import arena_achievement_service
    for row in rows:
        arena_achievement_service.check_and_unlock_arena_achievements(db, row.user_id)

    # Phase 15 — seasonal missions "disappear automatically at season end"
    # (spec): mark any still-active/completed-but-unclaimed row for THIS
    # season as expired. Claimed rows are left untouched (history).
    from app.services.competitive import mission_service
    period_key = mission_service.period_key_for("seasonal", season_id=season.id)
    from app.models.liveops import ArenaPlayerMission
    stale_rows = db.exec(
        select(ArenaPlayerMission)
        .where(ArenaPlayerMission.period_key == period_key)
        .where(ArenaPlayerMission.status.in_(["active", "completed"]))
    ).all()
    for stale in stale_rows:
        stale.status = "expired"
        db.add(stale)
    db.commit()

    season.status = "ended"
    season.updated_at = _now()
    db.add(season)
    db.commit()
    logger.info("competitive season ended: id=%s name=%s participants=%s", season.id, season.name, len(rows))

    # 2. Generate rewards (automatic).
    reward_service.distribute_season_rewards(db, season_id)

    # 3. Reset ratings per the configured strategy.
    strategy = season.reset_strategy
    percentage = season.reset_percentage if season.reset_percentage is not None else settings_row.competitive_season_default_reset_percentage
    for row in rows:
        stats = db.get(CompetitiveStatistics, row.user_id)
        if stats is None:
            continue
        stats.rating = _reset_rating(
            stats.rating, strategy=strategy, percentage=percentage,
            initial=settings_row.competitive_mmr_initial_rating, floor=settings_row.competitive_mmr_floor,
        )
        new_league = ranking_service.get_league_for_rating(db, stats.rating)
        stats.league_id = new_league.id if new_league else None
        stats.rank_tier = ranking_service.league_slug(new_league)
        # Phase 13 — "Season Win Streak" resets with the season itself
        # (current_streak/highest_rating are lifetime and deliberately
        # untouched by a season reset — only this season-scoped counter is).
        stats.season_best_streak = 0
        db.add(stats)
    db.commit()
    logger.info("competitive season reset applied: id=%s strategy=%s percentage=%s players=%s", season.id, strategy, percentage, len(rows))

    # 4. Auto-activate the next queued season, if any.
    if auto_activate_next:
        next_season = db.exec(
            select(CompetitiveSeason)
            .where(CompetitiveSeason.status == "upcoming")
            .order_by(CompetitiveSeason.starts_at.asc())
        ).first()
        if next_season is not None:
            activate_season(db, next_season.id)

    return season


def check_and_trigger_season_end(db: Session) -> Optional[UUID]:
    """Celery beat entry point — ends the active season once its ends_at has
    elapsed. Never silently lets a season run forever past its configured end."""
    season = get_active_season(db)
    if season is None or season.ends_at > _now():
        return None
    end_season(db, season.id)
    return season.id


def check_and_notify_ending_soon(db: Session) -> int:
    """Celery beat entry point — notifies every season participant once,
    when the active season enters its configured 'ending soon' window."""
    season = get_active_season(db)
    if season is None:
        return 0
    settings_row = _settings(db)
    window = settings_row.competitive_season_ending_soon_hours
    from datetime import timedelta
    if season.ends_at - _now() > timedelta(hours=window):
        return 0

    user_ids = list(db.exec(
        select(CompetitiveSeasonStats.user_id).where(CompetitiveSeasonStats.season_id == season.id)
    ).all())
    sent = 0
    for user_id in user_ids:
        result = emit(
            db, event_type="competitive_season_ending_soon", user_id=user_id,
            data={"season_id": str(season.id), "ends_at": season.ends_at.isoformat()},
            dedup_key=f"competitive_season_ending_soon:{season.id}:{user_id}",
        )
        if result is not None:
            sent += 1
    return sent
