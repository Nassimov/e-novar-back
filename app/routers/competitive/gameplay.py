from __future__ import annotations

"""Competitive Arena — Gameplay API (Phase 3).

The backend is the only source of truth: every endpoint here returns (or
mutates then returns) a MatchStateResponse computed server-side — the
frontend never calculates scores, timers, or winners itself.
"""

from typing import Any, Dict, List
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.competitive import CompetitiveMatchQuestion, CompetitiveQuestionAttempt
from app.models.practice import Question, QuestionChoice
from app.models.profile import Profile
from app.schemas.competitive import MatchStateResponse, SubmitAnswerRequest
from app.services.competitive import gameplay_service, match_service

router = APIRouter(tags=["Competitive"])


@router.get("/matches/{match_id}/gameplay/state", response_model=MatchStateResponse)
def get_gameplay_state(
    match_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The single endpoint reconnection/refresh relies on — restores
    current question, remaining timer, my own answer status, and running
    scores, all computed fresh from server state."""
    user_id = UUID(current_user["id"])
    match = match_service.get_match_or_404(db, match_id)
    participants = match_service.get_participants(db, match_id)
    match_service.assert_can_view(match, participants, user_id)
    return gameplay_service.compute_state(db, match, viewer_id=user_id)


@router.post("/matches/{match_id}/gameplay/answer", response_model=MatchStateResponse)
def submit_gameplay_answer(
    match_id: UUID,
    payload: SubmitAnswerRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])

    from app.core.rate_limiter import check_rate_limit
    from app.models.admin import PlatformSettings
    settings_row = db.get(PlatformSettings, True) or PlatformSettings()
    check_rate_limit(
        key=f"arena_rl:answer_submit:{user_id}", limit=settings_row.rate_limit_answer_submit_per_10s,
        message="Trop de réponses soumises. Merci de patienter.",
    )

    match = match_service.get_match_or_404(db, match_id)
    gameplay_service.submit_answer(
        db, match, user_id=user_id,
        match_question_id=payload.match_question_id, selected_choice_ids=payload.selected_choice_ids,
    )
    db.refresh(match)
    return gameplay_service.compute_state(db, match, viewer_id=user_id)


@router.post("/matches/{match_id}/gameplay/leave", response_model=MatchStateResponse)
def leave_gameplay_match(
    match_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Voluntary quit — immediate forfeit, distinct from the disconnect-
    timeout forfeit (which gives a reconnect grace period first)."""
    user_id = UUID(current_user["id"])
    match = match_service.get_match_or_404(db, match_id)
    gameplay_service.leave_match(db, match, user_id=user_id)
    db.refresh(match)
    return gameplay_service.compute_state(db, match, viewer_id=user_id)


@router.get("/matches/{match_id}/replay")
def get_match_replay(
    match_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[Dict[str, Any]]:
    """Immutable by construction — assembled straight from the authoritative
    match_questions/question_attempts tables (never a separate mutable copy).
    Only available once the match has finished revealing each question (no
    early leak of unrevealed answers)."""
    user_id = UUID(current_user["id"])
    match = match_service.get_match_or_404(db, match_id)
    participants = match_service.get_participants(db, match_id)
    match_service.assert_can_view(match, participants, user_id)

    mqs = db.exec(
        select(CompetitiveMatchQuestion)
        .where(CompetitiveMatchQuestion.match_id == match_id)
        .where(CompetitiveMatchQuestion.revealed_at.is_not(None))
        .order_by(CompetitiveMatchQuestion.position)
    ).all()

    replay = []
    for mq in mqs:
        question = db.get(Question, mq.question_id)
        choices = db.exec(select(QuestionChoice).where(QuestionChoice.question_id == mq.question_id)).all()
        attempts = db.exec(
            select(CompetitiveQuestionAttempt).where(CompetitiveQuestionAttempt.match_question_id == mq.id)
        ).all()
        answers = []
        for a in attempts:
            profile = db.get(Profile, a.user_id)
            answers.append({
                "user_id": str(a.user_id),
                "full_name": profile.full_name if profile else None,
                "selected_choice_ids": [str(c) for c in a.selected_choice_ids],
                "is_correct": a.is_correct,
                "points_earned": a.points_earned,
                "response_time_ms": a.response_time_ms,
            })
        replay.append({
            "position": mq.position,
            "statement": question.statement if question else "",
            "explanation": question.explanation if question else "",
            "choices": [{"id": str(c.id), "label": c.label, "text": c.text, "is_correct": c.is_correct} for c in choices],
            "answers": answers,
        })
    return replay
