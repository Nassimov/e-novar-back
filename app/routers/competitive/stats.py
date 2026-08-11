from __future__ import annotations

"""Competitive Arena — Statistics API (Phase 1: zero-state only)."""

from typing import Any, Dict
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.dependencies import get_current_user, get_db
from app.models.competitive import CompetitiveLeague
from app.schemas.competitive import LeagueOut, StatisticsResponse
from app.services.competitive import leaderboard_service, ranking_service, season_service, statistics_service

router = APIRouter(tags=["Competitive"])


@router.get("/stats/me", response_model=StatisticsResponse)
def get_my_stats(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    stats = statistics_service.get_or_create_statistics(db, user_id)
    # rank_tier is stored (and kept current by ranking_service.apply_match_result
    # after every match, since Phase 7), but always re-derive here too so it
    # can never drift out of sync if an admin edits the league ladder.
    stats.rank_tier = ranking_service.get_rank_tier(db, stats.rating)

    # Phase 7 — populate the ranking fields this endpoint used to leave at
    # their None/default. Reuses the exact same helpers as
    # app/routers/competitive/ranking.py's GET /profile/ranking, never
    # duplicates the ranking SQL.
    league = db.get(CompetitiveLeague, stats.league_id) if stats.league_id else None
    active_season = season_service.get_active_season(db)
    response = StatisticsResponse.model_validate(stats)
    response.league = LeagueOut.model_validate(league) if league else None
    response.global_rank = leaderboard_service.get_global_rank(db, user_id)
    response.active_season_id = active_season.id if active_season else None
    response.season_rank = (
        leaderboard_service.get_season_rank(db, active_season.id, user_id) if active_season else None
    )
    return response
