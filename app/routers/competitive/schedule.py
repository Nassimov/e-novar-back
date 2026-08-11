from __future__ import annotations

"""Competitive Arena — Scheduling API (Phase 2)."""

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session

from app.dependencies import get_current_user, get_db
from app.services.competitive import match_service, scheduling_service

router = APIRouter(tags=["Competitive"])


class ProposeSlotRequest(BaseModel):
    proposed_at_time: datetime
    timezone_label: Optional[str] = None


class ProposalResponse(BaseModel):
    id: UUID
    match_id: UUID
    proposed_by: UUID
    proposed_at_time: datetime
    timezone_label: Optional[str] = None
    status: str
    created_at: datetime
    responded_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


@router.post("/matches/{match_id}/schedule/propose", response_model=ProposalResponse, status_code=201)
def propose_slot(
    match_id: UUID,
    payload: ProposeSlotRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    match = match_service.get_match_or_404(db, match_id)
    proposal = scheduling_service.propose_slot(
        db, match, proposed_by=user_id,
        proposed_at_time=payload.proposed_at_time, timezone_label=payload.timezone_label,
    )
    return proposal


@router.get("/matches/{match_id}/schedule/proposals", response_model=List[ProposalResponse])
def list_proposals(
    match_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    match = match_service.get_match_or_404(db, match_id)
    participants = match_service.get_participants(db, match_id)
    match_service.assert_can_view(match, participants, user_id)
    return scheduling_service.list_proposals(db, match_id)


@router.post("/schedule/proposals/{proposal_id}/accept", response_model=ProposalResponse)
def accept_proposal(
    proposal_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    proposal = scheduling_service.get_proposal_or_404(db, proposal_id)
    scheduling_service.accept_proposal(db, proposal, user_id=user_id)
    db.refresh(proposal)
    return proposal


@router.post("/schedule/proposals/{proposal_id}/decline", response_model=ProposalResponse)
def decline_proposal(
    proposal_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    proposal = scheduling_service.get_proposal_or_404(db, proposal_id)
    proposal = scheduling_service.decline_proposal(db, proposal, user_id=user_id)
    return proposal
