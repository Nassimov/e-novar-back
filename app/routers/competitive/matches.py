from __future__ import annotations

"""
Competitive Arena — Matches API (Phase 1: foundation only, no gameplay).

All endpoints require an authenticated profile and enforce ownership/
participation server-side — the frontend's claims are never trusted.
"""

from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session

from app.dependencies import get_current_user, get_db
from app.models.profile import Profile
from app.schemas.competitive import MatchCreateRequest, MatchResponse, ParticipantResponse
from app.services.competitive import invitation_service, match_service, question_engine

router = APIRouter(tags=["Competitive"])


def _to_match_response(db: Session, match) -> MatchResponse:
    participants = match_service.get_participants(db, match.id)
    enriched: List[ParticipantResponse] = []
    for p in participants:
        profile = db.get(Profile, p.user_id)
        enriched.append(
            ParticipantResponse(
                id=p.id,
                user_id=p.user_id,
                full_name=profile.full_name if profile else None,
                avatar_url=profile.avatar_url if profile else None,
                role=p.role,
                is_ready=p.is_ready,
                result=p.result,
                score=p.score,
            )
        )
    return MatchResponse(**match.model_dump(), participants=enriched)


@router.post("/matches", response_model=MatchResponse, status_code=status.HTTP_201_CREATED)
def create_match(
    payload: MatchCreateRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    creator_id = UUID(current_user["id"])
    match = match_service.create_match(
        db,
        creator_id=creator_id,
        match_type=payload.match_type,
        subject_ids=payload.subject_ids,
        school_level_id=payload.school_level_id,
        difficulty=payload.difficulty,
        question_count=payload.question_count,
        visibility=payload.visibility,
        scheduled_at=payload.scheduled_at,
    )
    if payload.opponent_code:
        invitation_service.create_invitation(
            db, match, inviter_id=creator_id, opponent_code=payload.opponent_code
        )
        db.refresh(match)
    return _to_match_response(db, match)


@router.get("/matches", response_model=List[MatchResponse])
def list_matches(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    matches = match_service.list_my_matches(db, user_id, status_filter=status_filter)
    return [_to_match_response(db, m) for m in matches]


@router.get("/matches/{match_id}", response_model=MatchResponse)
def get_match(
    match_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    match = match_service.get_match_or_404(db, match_id)
    participants = match_service.get_participants(db, match_id)
    match_service.assert_can_view(match, participants, user_id)
    return _to_match_response(db, match)


@router.post("/matches/{match_id}/cancel", response_model=MatchResponse)
def cancel_match(
    match_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    match = match_service.get_match_or_404(db, match_id)
    match = match_service.cancel_match(db, match, user_id=user_id)
    return _to_match_response(db, match)


@router.get("/matches/config/question-availability")
def question_availability(
    subject_ids: List[UUID] = Query(default_factory=list),
    school_level_id: Optional[UUID] = Query(default=None),
    difficulty: Optional[str] = Query(default=None),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Feasibility check surfaced in the match-creation UI (future phase) —
    real query against the existing question bank, never mocked."""
    count = question_engine.count_available_questions(
        db, subject_ids=subject_ids, school_level_id=school_level_id, difficulty=difficulty
    )
    return {"available_questions": count}
