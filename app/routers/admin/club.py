from __future__ import annotations

"""
Admin oversight — Clash Club, Competitive Arena Phase 11, Part A.

Mounted at prefix="/api/admin/clubs" (see app/main.py) — a new dedicated
file/router, mirroring app/routers/admin/competitive.py's existing
Tournament/Battle-Royale admin CRUD sections' style, but split into its own
module since Club is its own domain (not a competitive_matches row) — same
reasoning as app/models/club.py / app/services/club/ being their own
namespaces rather than folded into the existing competitive.py files.
"""

import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlmodel import Session, func, select

from app.dependencies import get_admin_user, get_db
from app.models.club import Club, ClubAchievement
from app.models.competitive import CompetitiveMatch
from app.schemas.club import (
    AdminClubCreateRequest,
    AdminClubRenameRequest,
    AdminClubSuspendRequest,
    AdminReputationAdjustRequest,
    AdminRoleChangeRequest,
    ClubOut,
    ClubSearchResponse,
    ClubSeasonCreateRequest,
    ClubSeasonOut,
    ClubSeasonUpdateRequest,
    ClubUpdateRequest,
    CreateAchievementRequest,
    TransferOwnershipRequest,
)
from app.services.club import achievement_service, anti_abuse_service, battle_service, club_service, reputation_service, season_service
from app.services.club.permission_service import get_club_or_404

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Admin — Clubs"])


@router.post("", response_model=ClubOut, status_code=status.HTTP_201_CREATED)
def admin_create_club(
    payload: AdminClubCreateRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Admin-created club — bypasses eligibility/cost entirely."""
    club = club_service.create_club(
        db, owner_id=payload.owner_id, name=payload.name, tag=payload.tag, description=payload.description,
        logo_url=payload.logo_url, banner_url=payload.banner_url, privacy=payload.privacy,
        max_members=payload.max_members, subject_focus_id=payload.subject_focus_id,
        primary_language=payload.primary_language, region=payload.region,
        skip_eligibility=True, skip_cost=True, actor_email=_admin.get("email"),
    )
    return club_service.club_to_out(db, club)


@router.get("", response_model=ClubSearchResponse)
def admin_list_clubs(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Every status (including suspended/deleted) — unlike the player-facing
    /clubs search, which only ever shows status='active'."""
    query = select(Club)
    if status_filter:
        query = query.where(Club.status == status_filter)
    total = db.exec(select(func.count()).select_from(query.subquery())).one()
    query = query.order_by(Club.created_at.desc()).offset((page - 1) * size).limit(size)
    clubs = list(db.exec(query).all())
    return {"items": [club_service.club_to_out(db, c) for c in clubs], "total": total, "page": page, "size": size}


# ─── Part B: reputation / achievements / analytics ─────────────────────────
# NOTE: every literal single-segment path below (/achievements, /battles,
# /seasons, /anti-abuse/suspicious is 2-segment so it's safe either way)
# MUST be registered before the parametrized "/{club_id}" routes further
# down this file — FastAPI/Starlette matches routes in registration order,
# and "/{club_id}" would otherwise greedily match e.g. "/achievements" as a
# literal club_id (failing UUID parsing with a 422) before ever trying the
# real /achievements route.

@router.get("/anti-abuse/suspicious")
def admin_list_suspicious_clubs(
    limit: int = Query(default=100, ge=1, le=500), _admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db),
):
    return anti_abuse_service.list_suspicious_clubs(db, limit=limit)


@router.post("/achievements", status_code=status.HTTP_201_CREATED)
def admin_create_achievement(
    payload: CreateAchievementRequest, _admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db),
):
    achievement = ClubAchievement(
        code=payload.code, name=payload.name, description=payload.description, icon=payload.icon,
        criteria_type=payload.criteria_type, criteria_value=payload.criteria_value,
    )
    db.add(achievement)
    db.commit()
    db.refresh(achievement)
    return achievement


@router.get("/achievements")
def admin_list_achievements(_admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db)):
    return achievement_service.list_achievements(db, active_only=False)


@router.post("/achievements/{achievement_id}/deactivate")
def admin_deactivate_achievement(
    achievement_id: UUID, _admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db),
):
    achievement = db.get(ClubAchievement, achievement_id)
    if achievement is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Succès introuvable.")
    achievement.is_active = False
    db.add(achievement)
    db.commit()
    return {"deactivated": True}


# ─── Part C: club battles / seasons ─────────────────────────────────────────

@router.get("/battles")
def admin_list_battles(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1), size: int = Query(default=20, ge=1, le=100),
    _admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db),
):
    return battle_service.list_all_battles(db, status_filter=status_filter, page=page, size=size)


@router.post("/battles/{match_id}/cancel")
def admin_cancel_battle(
    match_id: UUID, reason: Optional[str] = None,
    _admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db),
):
    from fastapi import HTTPException
    match = db.get(CompetitiveMatch, match_id)
    if match is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bataille introuvable.")
    match = battle_service.admin_cancel_battle(db, match, reason=reason, actor_email=_admin.get("email"))
    return {"id": match.id, "status": match.status}


@router.get("/seasons", response_model=List[ClubSeasonOut])
def admin_list_seasons(_admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db)):
    return season_service.list_seasons(db)


@router.post("/seasons", response_model=ClubSeasonOut, status_code=status.HTTP_201_CREATED)
def admin_create_season(
    payload: ClubSeasonCreateRequest, _admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db),
):
    return season_service.create_season(
        db, name=payload.name, description=payload.description, starts_at=payload.starts_at, ends_at=payload.ends_at,
        reset_strategy=payload.reset_strategy, reset_percentage=payload.reset_percentage, rules=payload.rules,
        activate_now=payload.activate_now,
    )


@router.patch("/seasons/{season_id}", response_model=ClubSeasonOut)
def admin_update_season(
    season_id: UUID, payload: ClubSeasonUpdateRequest, _admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db),
):
    return season_service.update_season(db, season_id, **payload.model_dump())


@router.post("/seasons/{season_id}/activate", response_model=ClubSeasonOut)
def admin_activate_season(
    season_id: UUID, _admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db),
):
    return season_service.activate_season(db, season_id)


@router.post("/seasons/{season_id}/end", response_model=ClubSeasonOut)
def admin_end_season(
    season_id: UUID, _admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db),
):
    return season_service.end_season(db, season_id)


@router.get("/seasons/{season_id}/history")
def admin_get_season_history(
    season_id: UUID, page: int = Query(default=1, ge=1), size: int = Query(default=20, ge=1, le=100),
    _admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db),
):
    return season_service.get_season_history(db, season_id, page=page, size=size)


@router.get("/{club_id}", response_model=ClubOut)
def admin_get_club(
    club_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    club = get_club_or_404(db, club_id)
    return club_service.club_to_out(db, club)


@router.patch("/{club_id}", response_model=ClubOut)
def admin_update_club(
    club_id: UUID,
    payload: ClubUpdateRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Bypasses owner-only gating — an admin may edit any club directly."""
    club = get_club_or_404(db, club_id)
    club = club_service.update_club(db, club, actor_email=_admin.get("email"), **payload.model_dump())
    return club_service.club_to_out(db, club)


@router.post("/{club_id}/suspend", response_model=ClubOut)
def admin_suspend_club(
    club_id: UUID,
    payload: AdminClubSuspendRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    club = get_club_or_404(db, club_id)
    club = club_service.admin_suspend_club(db, club, reason=payload.reason, actor_email=_admin.get("email"))
    return club_service.club_to_out(db, club)


@router.post("/{club_id}/restore", response_model=ClubOut)
def admin_restore_club(
    club_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    club = get_club_or_404(db, club_id)
    club = club_service.admin_restore_club(db, club, actor_email=_admin.get("email"))
    return club_service.club_to_out(db, club)


@router.delete("/{club_id}", response_model=ClubOut)
def admin_delete_club(
    club_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Same soft-delete as the owner-initiated path (status='deleted') —
    audit trail preserved, never a hard DELETE (mirrors every other Phase
    7-10 admin 'delete' action on a permanent record)."""
    club = get_club_or_404(db, club_id)
    club = club_service.delete_club(db, club, actor_email=_admin.get("email"))
    return club_service.club_to_out(db, club)


@router.post("/{club_id}/rename", response_model=ClubOut)
def admin_rename_club(
    club_id: UUID,
    payload: AdminClubRenameRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    club = get_club_or_404(db, club_id)
    club = club_service.update_club(db, club, name=payload.name, tag=payload.tag, actor_email=_admin.get("email"))
    return club_service.club_to_out(db, club)


@router.post("/{club_id}/transfer-ownership", response_model=ClubOut)
def admin_transfer_ownership(
    club_id: UUID,
    payload: TransferOwnershipRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Admin override — no current-owner consent needed."""
    club = get_club_or_404(db, club_id)
    club = club_service.admin_transfer_ownership(db, club, payload.new_owner_user_id, actor_email=_admin.get("email"))
    return club_service.club_to_out(db, club)


@router.get("/{club_id}/members")
def admin_list_members(
    club_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Admin view — includes banned members, unlike the player-facing
    roster."""
    get_club_or_404(db, club_id)
    return club_service.list_members(db, club_id, include_banned=True)


@router.post("/{club_id}/members/{user_id}/role")
def admin_set_member_role(
    club_id: UUID,
    user_id: UUID,
    payload: AdminRoleChangeRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    club = get_club_or_404(db, club_id)
    member = club_service.admin_set_role(db, club, user_id, payload.role, actor_email=_admin.get("email"))
    return {
        "id": member.id, "club_id": member.club_id, "user_id": member.user_id,
        "role": member.role, "status": member.status, "joined_at": member.joined_at,
    }


@router.post("/{club_id}/reputation/adjust", response_model=ClubOut)
def admin_adjust_reputation(
    club_id: UUID, payload: AdminReputationAdjustRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db),
):
    club = get_club_or_404(db, club_id)
    club = reputation_service.award_reputation(db, club, delta=payload.delta, reason=f"admin_adjustment: {payload.reason}")
    return club_service.club_to_out(db, club)


@router.get("/{club_id}/analytics")
def admin_get_analytics(
    club_id: UUID, _admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db),
):
    from app.services.club import analytics_service, stats_service
    return {"statistics": stats_service.get_club_statistics(db, club_id), "analytics": analytics_service.get_club_analytics(db, club_id)}
