from __future__ import annotations

"""
Club Season Manager — Competitive Arena Phase 11, Part C.

Mirrors app/services/competitive/season_service.py's shape (upcoming/active/
ended lifecycle, one-active-at-a-time, reset strategy) but for the entirely
independent club season track (ClubSeason, not CompetitiveSeason — a club's
season calendar need not align with the player-level one).

Unlike the player track, there is no separate "club_season_stats" table
incrementally updated per-battle — wins/losses/draws for a season are
derived at season-end directly from club_rating_history rows tagged with
that season_id (battle_service._apply_club_rating already stamps every row
with the currently-active club season), avoiding a second per-battle write
just to keep a parallel counter in sync. club_season_history is the
permanent archive (never deleted, survives even a later club deletion — see
migration 091's header)."""

import logging
from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlmodel import Session, func, select

from app.models.admin import PlatformSettings
from app.models.club import Club, ClubRatingHistory, ClubSeason, ClubSeasonHistory
from app.services.club import achievement_service
from app.services.notification_engine import emit

logger = logging.getLogger(__name__)


def _settings(db: Session) -> PlatformSettings:
    return db.get(PlatformSettings, True) or PlatformSettings()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get_active_season(db: Session) -> Optional[ClubSeason]:
    return db.exec(select(ClubSeason).where(ClubSeason.status == "active")).first()


def get_season_or_404(db: Session, season_id: UUID) -> ClubSeason:
    season = db.get(ClubSeason, season_id)
    if season is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Saison de club introuvable.")
    return season


def list_seasons(db: Session) -> List[ClubSeason]:
    return list(db.exec(select(ClubSeason).order_by(ClubSeason.starts_at.desc())).all())


def create_season(
    db: Session, *, name: str, description: Optional[str], starts_at: datetime, ends_at: datetime,
    reset_strategy: str, reset_percentage: Optional[int], rules: dict, activate_now: bool,
) -> ClubSeason:
    if reset_strategy not in ("soft", "hard", "percentage"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="reset_strategy invalide.")
    if ends_at <= starts_at:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="ends_at doit être après starts_at.")

    season = ClubSeason(
        name=name, description=description, starts_at=starts_at, ends_at=ends_at,
        reset_strategy=reset_strategy, reset_percentage=reset_percentage, rules=rules or {}, status="upcoming",
    )
    db.add(season)
    db.commit()
    db.refresh(season)
    if activate_now:
        season = activate_season(db, season.id)
    return season


def update_season(db: Session, season_id: UUID, **fields) -> ClubSeason:
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


def activate_season(db: Session, season_id: UUID) -> ClubSeason:
    target = get_season_or_404(db, season_id)
    if target.status == "ended":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cette saison est déjà terminée.")

    current = get_active_season(db)
    if current is not None and current.id != target.id:
        end_season(db, current.id, auto_activate_next=False)
        target = get_season_or_404(db, season_id)

    target.status = "active"
    target.updated_at = _now()
    db.add(target)
    db.commit()
    db.refresh(target)

    _notify_season_started(db, target)
    return target


def _notify_season_started(db: Session, season: ClubSeason) -> None:
    from app.models.club import ClubMember
    user_ids = list(db.exec(select(ClubMember.user_id).where(ClubMember.status == "active")).all())
    for user_id in user_ids:
        emit(
            db, event_type="club_season_ended", user_id=user_id,  # reuses the same template family; "new season" copy lives client-side by context
            data={"season_id": str(season.id), "season_name": season.name, "phase": "started"},
            dedup_key=f"club_new_season_started:{season.id}:{user_id}",
        )


def _reset_rating(rating: int, *, strategy: str, percentage: int, initial: int, floor: int) -> int:
    if strategy == "hard":
        return initial
    pct = 100 if strategy == "hard" else percentage
    new_rating = round(initial + (rating - initial) * (1 - pct / 100))
    return max(floor, new_rating)


def end_season(db: Session, season_id: UUID, *, auto_activate_next: bool = True) -> ClubSeason:
    season = get_season_or_404(db, season_id)
    if season.status == "ended":
        return season
    settings_row = _settings(db)

    club_ids = list(db.exec(
        select(ClubRatingHistory.club_id).where(ClubRatingHistory.season_id == season_id).distinct()
    ).all())

    ranked: List[Club] = []
    if club_ids:
        ranked = list(db.exec(select(Club).where(Club.id.in_(club_ids)).order_by(Club.rating.desc())).all())

    for idx, club in enumerate(ranked, start=1):
        wins = db.exec(select(func.count()).select_from(
            select(ClubRatingHistory).where(ClubRatingHistory.season_id == season_id)
            .where(ClubRatingHistory.club_id == club.id).where(ClubRatingHistory.result == "win").subquery()
        )).one()
        losses = db.exec(select(func.count()).select_from(
            select(ClubRatingHistory).where(ClubRatingHistory.season_id == season_id)
            .where(ClubRatingHistory.club_id == club.id).where(ClubRatingHistory.result == "loss").subquery()
        )).one()
        draws = db.exec(select(func.count()).select_from(
            select(ClubRatingHistory).where(ClubRatingHistory.season_id == season_id)
            .where(ClubRatingHistory.club_id == club.id).where(ClubRatingHistory.result == "draw").subquery()
        )).one()

        db.add(ClubSeasonHistory(
            season_id=season_id, club_id=club.id, club_name=club.name, final_rank=idx, final_rating=club.rating,
            wins=wins or 0, losses=losses or 0, draws=draws or 0,
        ))

        if idx == 1:
            achievement_service.check_and_grant(db, club, extra_flag="season_champion")
            from app.models.club import ClubTrophy
            db.add(ClubTrophy(club_id=club.id, trophy_type="season", title=f"Champion — {season.name}", season_id=season.id))

        from app.models.club import ClubMember
        members = db.exec(select(ClubMember.user_id).where(ClubMember.club_id == club.id).where(ClubMember.status == "active")).all()
        for uid in members:
            emit(
                db, event_type="club_season_ended", user_id=uid,
                context={"club_name": club.name}, data={"season_id": str(season_id), "final_rank": idx},
                dedup_key=f"club_season_ended:{season_id}:{club.id}:{uid}",
            )

    db.commit()

    season.status = "ended"
    season.updated_at = _now()
    db.add(season)
    db.commit()
    logger.info("club season ended: id=%s name=%s clubs=%s", season.id, season.name, len(ranked))

    strategy = season.reset_strategy
    percentage = season.reset_percentage if season.reset_percentage is not None else 50
    for club in ranked:
        club.rating = _reset_rating(club.rating, strategy=strategy, percentage=percentage, initial=1000, floor=settings_row.competitive_club_rating_floor)
        db.add(club)
    db.commit()

    if auto_activate_next:
        next_season = db.exec(select(ClubSeason).where(ClubSeason.status == "upcoming").order_by(ClubSeason.starts_at.asc())).first()
        if next_season is not None:
            activate_season(db, next_season.id)

    return season


def check_and_trigger_season_end(db: Session) -> Optional[UUID]:
    season = get_active_season(db)
    if season is None or season.ends_at > _now():
        return None
    end_season(db, season.id)
    return season.id


def get_season_history(db: Session, season_id: UUID, *, page: int = 1, size: int = 20) -> List[ClubSeasonHistory]:
    return list(db.exec(
        select(ClubSeasonHistory).where(ClubSeasonHistory.season_id == season_id)
        .order_by(ClubSeasonHistory.final_rank.asc()).offset((page - 1) * size).limit(size)
    ).all())
