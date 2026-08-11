from __future__ import annotations

"""
Clash Club — Competitive Arena Phase 11, Part A — Player-facing API.

Mounted at prefix="/api" (see app/main.py) with FULL paths declared on each
route decorator (/clubs, /clubs/{club_id}, /invitations/{invitation_id}/
respond, /profile/my-club) — the spec's endpoint list spans three distinct
path roots (clubs/*, invitations/*, profile/*), so a single "/api/clubs"
router prefix (which would have doubled up into /api/clubs/clubs/...) isn't
the right shape here; declaring full paths on one router file instead
reproduces every literal path from the spec exactly, the same way
app/routers/competitive/*.py's several files all share one "/api/competitive"
prefix but declare their own distinct sub-paths.
"""

from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session

from app.dependencies import get_current_user, get_db
from app.schemas.club import (
    ClubCreateRequest,
    ClubInvitationOut,
    ClubMemberOut,
    ClubOut,
    ClubSearchResponse,
    ClubUpdateRequest,
    InviteMemberRequest,
    KickOrBanRequest,
    MyClubInvitationOut,
    MyClubResponse,
    RespondInvitationRequest,
    RoleChangeRequest,
    TransferOwnershipRequest,
)
from app.services.club import club_service
from app.services.club.permission_service import get_club_or_404, require_permission

router = APIRouter(tags=["Clubs"])


@router.post("/clubs", response_model=ClubOut, status_code=status.HTTP_201_CREATED)
def create_club(
    payload: ClubCreateRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from app.services.competitive import feature_flags_service
    if not feature_flags_service.is_enabled(db, "clubs"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cette fonctionnalité est actuellement désactivée.")

    user_id = UUID(current_user["id"])
    club = club_service.create_club(
        db, owner_id=user_id, name=payload.name, tag=payload.tag, description=payload.description,
        logo_url=payload.logo_url, banner_url=payload.banner_url, privacy=payload.privacy,
        max_members=payload.max_members, subject_focus_id=payload.subject_focus_id,
        primary_language=payload.primary_language, region=payload.region,
    )
    return club_service.club_to_out(db, club)


@router.get("/clubs", response_model=ClubSearchResponse)
def search_clubs(
    q: Optional[str] = Query(default=None),
    subject_id: Optional[UUID] = Query(default=None),
    language: Optional[str] = Query(default=None),
    region: Optional[str] = Query(default=None),
    min_members: Optional[int] = Query(default=None, ge=0),
    sort: str = Query(default="newest"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    items, total = club_service.search_clubs(
        db, q=q, subject_id=subject_id, language=language, region=region, min_members=min_members,
        sort=sort, page=page, size=size,
    )
    return {"items": items, "total": total, "page": page, "size": size}


@router.get("/profile/my-club", response_model=MyClubResponse)
def get_my_club(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    club, membership = club_service.get_my_club(db, user_id)
    if club is None:
        return {"club": None, "membership": None}
    return {
        "club": club_service.club_to_out(db, club),
        "membership": {
            "id": membership.id, "club_id": membership.club_id, "user_id": membership.user_id,
            "full_name": None, "avatar_url": None, "role": membership.role, "status": membership.status,
            "joined_at": membership.joined_at, "banned_at": membership.banned_at, "banned_reason": membership.banned_reason,
        },
    }


@router.get("/profile/club-invitations", response_model=List[MyClubInvitationOut])
def get_my_invitations(
    status_filter: Optional[str] = Query(default="pending", alias="status"),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    return club_service.list_my_invitations(db, user_id, status_filter=status_filter)


@router.get("/clubs/{club_id}", response_model=ClubOut)
def get_club(
    club_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return club_service.get_club_profile(db, club_id)


@router.patch("/clubs/{club_id}", response_model=ClubOut)
def update_club(
    club_id: UUID,
    payload: ClubUpdateRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    club = get_club_or_404(db, club_id)
    require_permission(db, club_id, user_id, "edit_club")
    club = club_service.update_club(db, club, **payload.model_dump())
    return club_service.club_to_out(db, club)


@router.delete("/clubs/{club_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_club(
    club_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    club = get_club_or_404(db, club_id)
    require_permission(db, club_id, user_id, "delete_club")
    club_service.delete_club(db, club, actor_id=user_id)


@router.post("/clubs/{club_id}/join")
def join_club(
    club_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    club = get_club_or_404(db, club_id)
    result = club_service.join_club(db, club, user_id)
    return {
        "joined": result["joined"],
        "invitation_id": result["invitation"].id if result["invitation"] else None,
    }


@router.delete("/clubs/{club_id}/leave", status_code=status.HTTP_204_NO_CONTENT)
def leave_club(
    club_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    club = get_club_or_404(db, club_id)
    club_service.leave_club(db, club, user_id)


@router.post("/clubs/{club_id}/invitations", response_model=ClubInvitationOut, status_code=status.HTTP_201_CREATED)
def invite_member(
    club_id: UUID,
    payload: InviteMemberRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    club = get_club_or_404(db, club_id)
    return club_service.invite_member(db, club, user_id, payload.user_id, message=payload.message)


@router.get("/clubs/{club_id}/invitations", response_model=List[ClubInvitationOut])
def list_club_invitations(
    club_id: UUID,
    status_filter: Optional[str] = Query(default="pending", alias="status"),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    get_club_or_404(db, club_id)
    require_permission(db, club_id, user_id, "invite")
    return club_service.list_invitations(db, club_id, status_filter=status_filter)


@router.post("/invitations/{invitation_id}/respond")
def respond_to_invitation(
    invitation_id: UUID,
    payload: RespondInvitationRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    invitation = club_service.get_invitation_or_404(db, invitation_id)
    result = club_service.respond_to_invitation(db, invitation, user_id, payload.accept)
    return {
        "status": result["invitation"].status,
        "joined": result["membership"] is not None,
    }


@router.get("/clubs/{club_id}/members", response_model=List[ClubMemberOut])
def list_members(
    club_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    get_club_or_404(db, club_id)
    return club_service.list_members(db, club_id)


@router.post("/clubs/{club_id}/members/{user_id}/kick", status_code=status.HTTP_204_NO_CONTENT)
def kick_member(
    club_id: UUID,
    user_id: UUID,
    payload: KickOrBanRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    actor_id = UUID(current_user["id"])
    club = get_club_or_404(db, club_id)
    club_service.kick_member(db, club, actor_id, user_id, reason=payload.reason)


@router.post("/clubs/{club_id}/members/{user_id}/ban", status_code=status.HTTP_204_NO_CONTENT)
def ban_member(
    club_id: UUID,
    user_id: UUID,
    payload: KickOrBanRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    actor_id = UUID(current_user["id"])
    club = get_club_or_404(db, club_id)
    club_service.ban_member(db, club, actor_id, user_id, reason=payload.reason)


@router.post("/clubs/{club_id}/members/{user_id}/promote", response_model=ClubMemberOut)
def promote_member(
    club_id: UUID,
    user_id: UUID,
    payload: RoleChangeRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    actor_id = UUID(current_user["id"])
    club = get_club_or_404(db, club_id)
    member = club_service.promote_member(db, club, actor_id, user_id, payload.role)
    return {
        "id": member.id, "club_id": member.club_id, "user_id": member.user_id, "full_name": None, "avatar_url": None,
        "role": member.role, "status": member.status, "joined_at": member.joined_at,
        "banned_at": member.banned_at, "banned_reason": member.banned_reason,
    }


@router.post("/clubs/{club_id}/members/{user_id}/demote", response_model=ClubMemberOut)
def demote_member(
    club_id: UUID,
    user_id: UUID,
    payload: RoleChangeRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    actor_id = UUID(current_user["id"])
    club = get_club_or_404(db, club_id)
    member = club_service.demote_member(db, club, actor_id, user_id, payload.role)
    return {
        "id": member.id, "club_id": member.club_id, "user_id": member.user_id, "full_name": None, "avatar_url": None,
        "role": member.role, "status": member.status, "joined_at": member.joined_at,
        "banned_at": member.banned_at, "banned_reason": member.banned_reason,
    }


@router.post("/clubs/{club_id}/transfer-ownership", response_model=ClubOut)
def transfer_ownership(
    club_id: UUID,
    payload: TransferOwnershipRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    actor_id = UUID(current_user["id"])
    club = get_club_or_404(db, club_id)
    club = club_service.transfer_ownership(db, club, actor_id, payload.new_owner_user_id)
    return club_service.club_to_out(db, club)
