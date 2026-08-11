from __future__ import annotations

"""Competitive Arena — Invitations API (Phase 1)."""

from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session

from app.dependencies import get_current_user, get_db
from app.models.profile import Profile
from app.schemas.competitive import InvitationCreateRequest, InvitationResponse
from app.services.competitive import invitation_service, match_service

router = APIRouter(tags=["Competitive"])


def _to_invitation_response(db: Session, invitation) -> InvitationResponse:
    inviter = db.get(Profile, invitation.inviter_id)
    invitee = db.get(Profile, invitation.invitee_id)
    return InvitationResponse(
        **invitation.model_dump(),
        inviter_name=inviter.full_name if inviter else None,
        invitee_name=invitee.full_name if invitee else None,
    )


@router.post("/invitations", response_model=InvitationResponse, status_code=201)
def create_invitation(
    payload: InvitationCreateRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    inviter_id = UUID(current_user["id"])

    from app.core.rate_limiter import check_rate_limit
    from app.models.admin import PlatformSettings
    settings_row = db.get(PlatformSettings, True) or PlatformSettings()
    check_rate_limit(
        key=f"arena_rl:invitation_create:{inviter_id}", limit=settings_row.rate_limit_invitation_create_per_10s,
        message="Trop d'invitations envoyées. Merci de patienter.",
    )

    match = match_service.get_match_or_404(db, payload.match_id)
    invitation = invitation_service.create_invitation(
        db, match, inviter_id=inviter_id, opponent_code=payload.opponent_code
    )
    return _to_invitation_response(db, invitation)


@router.get("/invitations", response_model=Dict[str, List[InvitationResponse]])
def list_invitations(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    received = invitation_service.list_received(db, user_id)
    sent = invitation_service.list_sent(db, user_id)
    return {
        "received": [_to_invitation_response(db, i) for i in received],
        "sent": [_to_invitation_response(db, i) for i in sent],
    }


@router.get("/invitations/history")
def list_invitation_history(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Full paginated inbox — every status, newest first (spec: 'History
    must be paginated. Newest first.')."""
    user_id = UUID(current_user["id"])
    items, total = invitation_service.list_history(db, user_id, status_filter=status_filter, page=page, size=size)
    return {
        "items": [_to_invitation_response(db, i) for i in items],
        "total": total,
        "page": page,
        "size": size,
    }


@router.post("/invitations/{invitation_id}/accept", response_model=InvitationResponse)
def accept_invitation(
    invitation_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    invitation = invitation_service.get_invitation_or_404(db, invitation_id)
    invitation_service.accept_invitation(db, invitation, user_id=user_id)
    db.refresh(invitation)
    return _to_invitation_response(db, invitation)


@router.post("/invitations/{invitation_id}/decline", response_model=InvitationResponse)
def decline_invitation(
    invitation_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    invitation = invitation_service.get_invitation_or_404(db, invitation_id)
    invitation = invitation_service.decline_invitation(db, invitation, user_id=user_id)
    return _to_invitation_response(db, invitation)


@router.post("/invitations/{invitation_id}/cancel", response_model=InvitationResponse)
def cancel_invitation(
    invitation_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    invitation = invitation_service.get_invitation_or_404(db, invitation_id)
    invitation = invitation_service.cancel_invitation(db, invitation, user_id=user_id)
    return _to_invitation_response(db, invitation)
