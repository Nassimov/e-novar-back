from __future__ import annotations

"""
Player-facing Ranking API — Competitive Arena Phase 7.

Read-only surface over the League Engine / Season Manager / Leaderboard
Engine built for admins in app/routers/admin/competitive.py — everything
here mirrors the same underlying services, never recomputes ranking logic
of its own (spec: "Never compute rankings on frontend" — the backend, and
only the backend, is the source of truth; this module is just its read
API for players).
"""

from typing import List, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.competitive import CompetitiveLeague, CompetitiveRatingHistory, CompetitiveSeasonStats
from app.models.profile import Profile
from app.schemas.competitive import (
    LeaderboardEntryOut,
    LeaderboardResponse,
    LeagueOut,
    ProfileRankingResponse,
    RatingHistoryEntryOut,
    SeasonOut,
    SeasonStatsOut,
)
from app.services.competitive import leaderboard_service, ranking_service, season_service, statistics_service

router = APIRouter(tags=["Competitive"])


# ─── League Engine (public ladder) ──────────────────────────────────────────

@router.get("/leagues", response_model=List[LeagueOut])
def get_public_leagues(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return list(
        db.exec(
            select(CompetitiveLeague)
            .where(CompetitiveLeague.visible.is_(True))
            .order_by(CompetitiveLeague.sort_order.asc())
        ).all()
    )


# ─── Season Manager (read-only) ─────────────────────────────────────────────

@router.get("/seasons/active", response_model=SeasonOut)
def get_active_season(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    season = season_service.get_active_season(db)
    if season is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Aucune saison active actuellement.")
    return season


@router.get("/seasons", response_model=List[SeasonOut])
def list_seasons_history(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return season_service.list_seasons(db)  # already ordered starts_at desc — most recent first


# ─── Leaderboard Engine ──────────────────────────────────────────────────────

@router.get("/leaderboard", response_model=LeaderboardResponse)
def get_leaderboard(
    period: Literal["daily", "weekly", "monthly", "season", "all_time"] = Query(default="season"),
    scope: Literal["global", "national"] = Query(default="global"),
    season_id: Optional[UUID] = Query(default=None),
    subject_id: Optional[UUID] = Query(default=None),
    difficulty: Optional[str] = Query(default=None),
    league_id: Optional[UUID] = Query(default=None),
    language: Optional[str] = Query(default=None),
    wilaya: Optional[str] = Query(default=None),
    school_level: Optional[str] = Query(default=None, description="StudentProfile.level_main — spec's 'Academic Level' filter"),
    sort: Optional[str] = Query(default=None, description="rating|wins|accuracy|streak|battle_royale_wins|tournament_wins|club_wins"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """`scope` is accepted for forward-compatibility only — "national"
    currently reuses the global board verbatim (see leaderboard_service's
    module docstring: E-NOVAR is a single-country platform)."""
    from app.core.rate_limiter import check_rate_limit
    from app.models.admin import PlatformSettings
    settings_row = db.get(PlatformSettings, True) or PlatformSettings()
    check_rate_limit(
        key=f"arena_rl:leaderboard:{current_user['id']}", limit=settings_row.rate_limit_leaderboard_refresh_per_10s,
        message="Trop de rafraîchissements du classement. Merci de patienter.",
    )

    resolved_season_id = season_id
    if period == "season" and resolved_season_id is None:
        active = season_service.get_active_season(db)
        resolved_season_id = active.id if active else None

    items, total = leaderboard_service.get_leaderboard(
        db, period=period, season_id=resolved_season_id,
        subject_id=subject_id, difficulty=difficulty, league_id=league_id,
        language=language, wilaya=wilaya, school_level_id=school_level, sort=sort,
        page=page, size=size,
    )
    return LeaderboardResponse(
        period=period, season_id=resolved_season_id,
        items=[LeaderboardEntryOut(**item) for item in items],
        total=total, page=page, size=size,
    )


# ─── Personal ranking / rating history ──────────────────────────────────────

@router.get("/profile/ranking", response_model=ProfileRankingResponse)
def get_profile_ranking(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    stats = statistics_service.get_or_create_statistics(db, user_id)
    league = db.get(CompetitiveLeague, stats.league_id) if stats.league_id else None
    global_rank = leaderboard_service.get_global_rank(db, user_id)
    win_rate = round(stats.wins / stats.matches_played, 4) if stats.matches_played else 0.0

    current_season: Optional[SeasonStatsOut] = None
    active_season = season_service.get_active_season(db)
    if active_season is not None:
        season_row = db.exec(
            select(CompetitiveSeasonStats)
            .where(CompetitiveSeasonStats.season_id == active_season.id)
            .where(CompetitiveSeasonStats.user_id == user_id)
        ).first()
        if season_row is not None:
            season_league = db.get(CompetitiveLeague, season_row.league_id) if season_row.league_id else None
            current_season = SeasonStatsOut(
                season_id=active_season.id, season_name=active_season.name,
                mmr=season_row.mmr,
                league=LeagueOut.model_validate(season_league) if season_league else None,
                matches_played=season_row.matches_played, wins=season_row.wins,
                losses=season_row.losses, draws=season_row.draws,
                accuracy_pct=season_row.accuracy_pct, avg_response_time_ms=season_row.avg_response_time_ms,
                final_rank=season_row.final_rank,
                season_rank=leaderboard_service.get_season_rank(db, active_season.id, user_id),
            )

    from app.models.admin import PlatformSettings
    settings_row = db.get(PlatformSettings, True)
    placement_required = settings_row.competitive_placement_matches_required if settings_row else 10

    is_eligible = True
    try:
        ranking_service.check_ranked_eligibility(db, user_id)
    except HTTPException:
        is_eligible = False

    return ProfileRankingResponse(
        user_id=user_id, rating=stats.rating, highest_rating=stats.highest_rating,
        league=LeagueOut.model_validate(league) if league else None,
        best_rank_tier=stats.best_rank_tier, global_rank=global_rank,
        win_rate=win_rate, current_streak=stats.current_streak, longest_streak=stats.longest_streak,
        current_season=current_season,
        placement_complete=stats.placement_complete, placement_matches_played=stats.placement_matches_played,
        placement_matches_required=placement_required, fair_play_score=stats.fair_play_score,
        season_best_streak=stats.season_best_streak, is_ranked_eligible=is_eligible,
    )


@router.get("/profile/rating-history", response_model=List[RatingHistoryEntryOut])
def get_rating_history(
    limit: int = Query(default=20, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    rows = list(
        db.exec(
            select(CompetitiveRatingHistory)
            .where(CompetitiveRatingHistory.user_id == user_id)
            .order_by(CompetitiveRatingHistory.created_at.desc())
            .limit(limit)
        ).all()
    )
    if not rows:
        return []

    opponent_ids = {r.opponent_id for r in rows if r.opponent_id}
    league_ids = {r.league_before_id for r in rows if r.league_before_id} | {r.league_after_id for r in rows if r.league_after_id}
    profiles = {p.id: p for p in db.exec(select(Profile).where(Profile.id.in_(opponent_ids))).all()} if opponent_ids else {}
    leagues = {l.id: l for l in db.exec(select(CompetitiveLeague).where(CompetitiveLeague.id.in_(league_ids))).all()} if league_ids else {}

    return [
        RatingHistoryEntryOut(
            id=r.id, match_id=r.match_id, opponent_id=r.opponent_id,
            opponent_name=profiles[r.opponent_id].full_name if r.opponent_id in profiles else None,
            result=r.result, rating_before=r.rating_before, rating_after=r.rating_after, delta=r.delta,
            league_before=ranking_service.league_label(leagues[r.league_before_id]) if r.league_before_id in leagues else None,
            league_after=ranking_service.league_label(leagues[r.league_after_id]) if r.league_after_id in leagues else None,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.get("/profile/rating-history-graph")
def get_rating_history_graph(
    period: Literal["daily", "weekly", "monthly", "season", "all_time"] = Query(default="all_time"),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Phase 13 — 'Rating History... Generate rating graph. Display: Daily/
    Weekly/Monthly/Season/All Time.' Distinct from /profile/rating-history
    above (a flat recent-matches list for the Match History table) — this
    buckets CompetitiveRatingHistory by calendar period and returns the
    LAST rating_after within each bucket (chart-ready series), scoped to
    the active season when period='season'."""
    from datetime import datetime, timedelta, timezone

    user_id = UUID(current_user["id"])
    query = select(CompetitiveRatingHistory).where(CompetitiveRatingHistory.user_id == user_id)

    if period == "season":
        active = season_service.get_active_season(db)
        if active is None:
            return {"period": period, "points": []}
        query = query.where(CompetitiveRatingHistory.season_id == active.id)
    elif period != "all_time":
        now = datetime.now(timezone.utc)
        since = {"daily": now - timedelta(days=1), "weekly": now - timedelta(days=7), "monthly": now - timedelta(days=30)}[period]
        query = query.where(CompetitiveRatingHistory.created_at >= since)

    rows = list(db.exec(query.order_by(CompetitiveRatingHistory.created_at.asc())).all())
    if not rows:
        return {"period": period, "points": []}

    def _bucket_key(dt) -> str:
        if period == "daily":
            return dt.strftime("%Y-%m-%dT%H:00")
        if period in ("weekly", "monthly", "all_time", "season"):
            return dt.strftime("%Y-%m-%d")
        return dt.isoformat()

    buckets: dict = {}
    for r in rows:
        buckets[_bucket_key(r.created_at)] = r.rating_after  # last write per bucket wins — rows are ASC-ordered
    points = [{"bucket": k, "rating": v} for k, v in buckets.items()]
    return {"period": period, "points": points}


@router.get("/hall-of-fame")
def get_hall_of_fame(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from app.services.competitive import hall_of_fame_service
    return hall_of_fame_service.get_hall_of_fame(db)
