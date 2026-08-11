from __future__ import annotations

"""Clash Club — Part B — Feed / Achievements / Trophies / Statistics /
Analytics API. Mounted at prefix="/api" (see app/main.py)."""

from typing import Any, Dict, List
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.club import ClubTrophy
from app.schemas.club import AchievementGrantOut, AchievementOut, ClubAnalyticsOut, ClubStatisticsOut, FeedEventOut, TrophyOut
from app.services.club import achievement_service, analytics_service, feed_service, stats_service
from app.services.club.permission_service import get_club_or_404, require_permission

router = APIRouter(tags=["Clubs"])


@router.get("/clubs/{club_id}/feed")
def get_feed(
    club_id: UUID, page: int = Query(default=1, ge=1), size: int = Query(default=30, ge=1, le=100),
    current_user: Dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db),
):
    get_club_or_404(db, club_id)
    return feed_service.list_feed(db, club_id, page=page, size=size)


@router.get("/clubs/{club_id}/achievements", response_model=List[AchievementGrantOut])
def get_club_achievements(
    club_id: UUID, current_user: Dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db),
):
    get_club_or_404(db, club_id)
    return achievement_service.list_grants(db, club_id)


@router.get("/achievements/catalogue", response_model=List[AchievementOut])
def get_achievement_catalogue(
    current_user: Dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db),
):
    return achievement_service.list_achievements(db)


@router.get("/clubs/{club_id}/trophies", response_model=List[TrophyOut])
def get_club_trophies(
    club_id: UUID, current_user: Dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db),
):
    get_club_or_404(db, club_id)
    return list(db.exec(select(ClubTrophy).where(ClubTrophy.club_id == club_id).order_by(ClubTrophy.awarded_at.desc())).all())


@router.get("/clubs/{club_id}/statistics", response_model=ClubStatisticsOut)
def get_statistics(
    club_id: UUID, current_user: Dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db),
):
    return stats_service.get_club_statistics(db, club_id)


@router.get("/clubs/{club_id}/analytics", response_model=ClubAnalyticsOut)
def get_analytics(
    club_id: UUID, current_user: Dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    require_permission(db, club_id, user_id, "view_analytics")
    return analytics_service.get_club_analytics(db, club_id)
