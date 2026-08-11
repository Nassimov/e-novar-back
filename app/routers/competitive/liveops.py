from __future__ import annotations

"""
Player-facing LiveOps API — Competitive Arena Phase 15 (Missions, Login
Rewards, Events, Community Challenges, Calendar). Mounted under
/api/competitive (see app/main.py).
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.competitive import CompetitiveSeason, CompetitiveTournament
from app.models.liveops import ArenaEvent
from app.services.competitive import event_service, login_service, mission_service, season_service
from app.services.competitive.feature_flags_service import require_feature

router = APIRouter(tags=["Competitive"], dependencies=[Depends(require_feature("liveops"))])


# ─── Missions ───────────────────────────────────────────────────────────────

@router.get("/missions")
def get_missions(
    period: str = Query(..., pattern="^(daily|weekly|monthly|seasonal|event)$"),
    event_id: Optional[UUID] = Query(default=None),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    season_id = None
    if period == "seasonal":
        active = season_service.get_active_season(db)
        if active is None:
            return {"items": []}
        season_id = active.id
    if period == "event" and event_id is None:
        return {"items": []}
    return {"items": mission_service.list_missions_view(db, user_id, period, season_id=season_id, event_id=event_id)}


@router.post("/missions/{player_mission_id}/claim")
def claim_mission(
    player_mission_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    return mission_service.claim_mission(db, user_id, player_mission_id)


@router.post("/missions/{player_mission_id}/reroll")
def reroll_mission(
    player_mission_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    return mission_service.reroll_mission(db, user_id, player_mission_id)


# ─── Login rewards / streak ────────────────────────────────────────────────

@router.post("/login/checkin")
def login_checkin(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    return login_service.checkin(db, user_id)


@router.get("/login/calendar")
def get_login_calendar(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    return login_service.get_calendar(db, user_id)


@router.post("/login/calendar/{day_number}/claim")
def claim_login_reward(
    day_number: int,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    return login_service.claim_day_reward(db, user_id, day_number)


# ─── Events / Happy Hour / Double Rewards / Community Challenges ──────────

@router.get("/events")
def list_events(
    status: Optional[str] = Query(default=None),
    event_type: Optional[str] = Query(default=None),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    statuses = [status] if status else ["scheduled", "active", "ended"]
    events = event_service.list_events(db, statuses=statuses, event_type=event_type)
    return {"items": [
        {
            "id": str(e.id), "event_type": e.event_type, "code": e.code, "name": e.name,
            "banner_url": e.banner_url, "description": e.description, "starts_at": e.starts_at,
            "ends_at": e.ends_at, "status": e.status, "config": e.config, "reward_config": e.reward_config,
        }
        for e in events
    ]}


@router.get("/events/{event_id}")
def get_event(
    event_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    e = event_service.get_event_or_404(db, event_id)
    payload: Dict[str, Any] = {
        "id": str(e.id), "event_type": e.event_type, "code": e.code, "name": e.name,
        "banner_url": e.banner_url, "description": e.description, "rules": e.rules,
        "starts_at": e.starts_at, "ends_at": e.ends_at, "status": e.status, "config": e.config,
        "reward_config": e.reward_config,
    }
    if e.event_type == "community_challenge":
        payload["community_progress"] = event_service.get_community_progress(db, event_id)
    return payload


@router.post("/events/{event_id}/join")
def join_event(
    event_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    row = event_service.join_event(db, user_id, event_id)
    return {"event_id": str(event_id), "joined_at": row.joined_at}


@router.post("/events/{event_id}/claim")
def claim_event_reward(
    event_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    return event_service.claim_event_reward(db, user_id, event_id)


@router.get("/events/{event_id}/leaderboard")
def get_event_leaderboard(
    event_id: UUID,
    club_id: Optional[UUID] = Query(default=None),
    wilaya: Optional[str] = Query(default=None),
    school_level_id: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=100),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    items, total = event_service.get_event_leaderboard(
        db, event_id, club_id=club_id, wilaya=wilaya, school_level_id=school_level_id, page=page, size=size,
    )
    return {"items": items, "total": total, "page": page, "size": size}


@router.get("/community-challenges")
def list_community_challenges(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    events = event_service.list_events(db, statuses=["active"], event_type="community_challenge")
    return {"items": [
        {
            "id": str(e.id), "name": e.name, "description": e.description,
            "progress": event_service.get_community_progress(db, e.id),
        }
        for e in events
    ]}


# ─── LiveOps Calendar ───────────────────────────────────────────────────────

@router.get("/liveops/calendar")
def get_liveops_calendar(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Computed, not stored — aggregates arena_events, competitive_seasons
    and competitive_tournaments, plus the static mission-reset cron
    schedule, rather than duplicating any of them into a parallel table
    (see migration 095's header)."""
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=30)

    events = db.exec(
        select(ArenaEvent).where(ArenaEvent.status.in_(["scheduled", "active", "ended"])).where(ArenaEvent.ends_at >= now - timedelta(days=7))
    ).all()
    seasons = db.exec(
        select(CompetitiveSeason).where(CompetitiveSeason.status.in_(["active", "upcoming"]))
    ).all()
    tournaments = db.exec(
        select(CompetitiveTournament)
        .where(CompetitiveTournament.status.in_(["published", "registration_open", "registration_closed", "bracket_generated", "running"]))
        .where(CompetitiveTournament.starts_at <= horizon)
    ).all()

    algiers = timezone(timedelta(hours=1))
    local_now = now.astimezone(algiers)
    next_daily_reset = (local_now + timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)
    days_until_monday = (7 - local_now.isoweekday() + 1) % 7 or 7
    next_weekly_reset = (local_now + timedelta(days=days_until_monday)).replace(hour=0, minute=1, second=0, microsecond=0)
    if local_now.month == 12:
        next_monthly_reset = local_now.replace(year=local_now.year + 1, month=1, day=1, hour=0, minute=1, second=0, microsecond=0)
    else:
        next_monthly_reset = local_now.replace(month=local_now.month + 1, day=1, hour=0, minute=1, second=0, microsecond=0)

    return {
        "events": [
            {"id": str(e.id), "name": e.name, "event_type": e.event_type, "status": e.status, "starts_at": e.starts_at, "ends_at": e.ends_at}
            for e in events
        ],
        "seasons": [
            {"id": str(s.id), "name": s.name, "status": s.status, "starts_at": s.starts_at, "ends_at": s.ends_at}
            for s in seasons
        ],
        "tournaments": [
            {"id": str(t.id), "name": t.name, "status": t.status, "starts_at": t.starts_at}
            for t in tournaments
        ],
        "mission_resets": {
            "next_daily_reset": next_daily_reset.astimezone(timezone.utc),
            "next_weekly_reset": next_weekly_reset.astimezone(timezone.utc),
            "next_monthly_reset": next_monthly_reset.astimezone(timezone.utc),
        },
    }
