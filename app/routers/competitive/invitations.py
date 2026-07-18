from __future__ import annotations

"""Competitive Arena — Invitations API (Phase 1)."""

from typing import Any, Dict, List
from uuid import UUID

from fastapi import APIRouter, Depends
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
