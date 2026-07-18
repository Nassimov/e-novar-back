from __future__ import annotations

"""Competitive Arena — Statistics API (Phase 1: zero-state only)."""

from typing import Any, Dict
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.dependencies import get_current_user, get_db
from app.schemas.competitive import StatisticsResponse
from app.services.competitive import ranking_service, statistics_service

router = APIRouter(tags=["Competitive"])


@router.get("/stats/me", response_model=StatisticsResponse)
def get_my_stats(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    stats = statistics_service.get_or_create_statistics(db, user_id)
    # rank_tier is stored, but always re-derive from rating so it can never
    # drift out of sync if the thresholds change in a future phase.
    stats.rank_tier = ranking_service.get_rank_tier(stats.rating)
    return StatisticsResponse.model_validate(stats)
