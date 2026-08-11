from __future__ import annotations

"""Clash Club — Part C — public Season / Leaderboard read API. Mounted at
prefix="/api" (see app/main.py). Admin CRUD for seasons lives in
app/routers/admin/club.py, mirroring how competitive_seasons splits
public-read vs. admin-write across two router files."""

from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from sqlmodel import Session

from app.dependencies import get_current_user, get_db
from app.schemas.club import ClubSeasonOut
from app.services.club import leaderboard_service, season_service

router = APIRouter(tags=["Clubs"])


@router.get("/club-seasons", response_model=List[ClubSeasonOut])
def list_seasons(current_user: Dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db)):
    return season_service.list_seasons(db)


@router.get("/club-seasons/active", response_model=Optional[ClubSeasonOut])
def get_active_season(current_user: Dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db)):
    return season_service.get_active_season(db)


@router.get("/club-seasons/{season_id}/leaderboard")
def get_season_leaderboard(
    season_id: UUID, page: int = Query(default=1, ge=1), size: int = Query(default=20, ge=1, le=100),
    current_user: Dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db),
):
    return leaderboard_service.get_season_leaderboard(db, season_id, page=page, size=size)


@router.get("/club-leaderboard")
def get_all_time_leaderboard(
    region: Optional[str] = Query(default=None), sort: str = Query(default="rating"),
    page: int = Query(default=1, ge=1), size: int = Query(default=20, ge=1, le=100),
    current_user: Dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db),
):
    return leaderboard_service.get_all_time_leaderboard(db, region=region, sort=sort, page=page, size=size)
