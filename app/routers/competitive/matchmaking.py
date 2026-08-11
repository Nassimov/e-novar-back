from __future__ import annotations

"""Competitive Arena — Matchmaking API (Phase 6). Join/leave/status/accept/
decline for the random-opponent queue. Pairing itself happens out-of-band
in the periodic Celery sweep (app/workers/competitive_tasks.py); this
router only ever manages the CALLER's own queue entry."""

from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.competitive import CompetitiveQueueEntry, CompetitiveStatistics
from app.routers.competitive.matches import _to_match_response
from app.schemas.competitive import JoinQueueRequest, MatchQualityBreakdown, MatchResponse, QueueStatusResponse
from app.services.competitive import match_service, matchmaking_service, queue_service

router = APIRouter(tags=["Competitive"])


def _to_status_response(db: Session, entry: CompetitiveQueueEntry) -> QueueStatusResponse:
    elapsed = int((datetime.now(timezone.utc) - entry.search_started_at).total_seconds())
    estimated_wait = queue_service.estimate_wait_seconds(db, entry)

    quality: Optional[MatchQualityBreakdown] = None
    if entry.status in ("matched", "accepted") and entry.matched_with_user_id is not None:
        other = db.exec(
            select(CompetitiveQueueEntry)
            .where(CompetitiveQueueEntry.proposal_group_id == entry.proposal_group_id)
            .where(CompetitiveQueueEntry.user_id == entry.matched_with_user_id)
        ).first()
        if other is not None:
            stats_mine = db.get(CompetitiveStatistics, entry.user_id)
            stats_other = db.get(CompetitiveStatistics, other.user_id)
            pool_ok = True  # already validated at proposal-creation time
            breakdown = matchmaking_service.compute_match_quality(
                entry, other, stats_a=stats_mine, stats_b=stats_other, question_pool_ok=pool_ok,
            )
            quality = MatchQualityBreakdown(
                rating_score=breakdown["rating_score"],
                language_score=breakdown["language_score"],
                win_rate_score=breakdown["win_rate_score"],
                response_time_score=breakdown["response_time_score"],
                question_pool_ok=breakdown["question_pool_ok"],
                overall_score=breakdown["overall_score"],
            )

    return QueueStatusResponse(
        queue_entry_id=entry.id,
        status=entry.status,
        mode=entry.mode,
        subject_id=entry.subject_id,
        school_level_id=entry.school_level_id,
        difficulty=entry.difficulty,
        language=entry.language,
        elapsed_seconds=elapsed,
        current_radius=entry.current_radius,
        estimated_wait_seconds=estimated_wait,
        proposal_group_id=entry.proposal_group_id,
        proposal_expires_at=entry.proposal_expires_at,
        matched_with_user_id=entry.matched_with_user_id,
        match_id=entry.match_id,
        match_quality=quality,
    )


@router.post("/queue/join", response_model=QueueStatusResponse)
def join_queue(
    payload: JoinQueueRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    entry = queue_service.join_queue(
        db,
        user_id=user_id,
        mode=payload.mode,
        subject_id=payload.subject_id,
        school_level_id=payload.school_level_id,
        difficulty=payload.difficulty,
        language=payload.language,
    )
    return _to_status_response(db, entry)


@router.post("/queue/leave", response_model=QueueStatusResponse)
def leave_queue(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    entry = queue_service.get_active_entry_or_404(db, user_id)
    entry = queue_service.leave_queue(db, entry, user_id=user_id)
    return _to_status_response(db, entry)


@router.get("/queue/status", response_model=QueueStatusResponse)
def get_queue_status(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Also doubles as the Reconnect Queue API — a page reload/reconnect
    just calls this again to resume showing the correct searching/matched
    UI instead of re-joining from scratch."""
    user_id = UUID(current_user["id"])
    entry = queue_service.get_active_entry_or_404(db, user_id)
    return _to_status_response(db, entry)


@router.post("/queue/{queue_entry_id}/accept", response_model=QueueStatusResponse)
def accept_match_proposal(
    queue_entry_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    entry = queue_service.get_entry_or_404(db, queue_entry_id)
    entry = matchmaking_service.accept_proposal(db, entry, user_id=user_id)
    return _to_status_response(db, entry)


@router.post("/queue/{queue_entry_id}/decline", response_model=QueueStatusResponse)
def decline_match_proposal(
    queue_entry_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    entry = queue_service.get_entry_or_404(db, queue_entry_id)
    entry = matchmaking_service.decline_proposal(db, entry, user_id=user_id)
    return _to_status_response(db, entry)


@router.get("/queue/{queue_entry_id}/match", response_model=MatchResponse)
def get_created_match(
    queue_entry_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Convenience lookup once QueueStatusResponse.match_id is set — hands
    back the ordinary MatchResponse so the frontend can route straight into
    the existing waiting-room page (identical to a direct-challenge duel
    from this point on)."""
    user_id = UUID(current_user["id"])
    entry = queue_service.get_entry_or_404(db, queue_entry_id)
    if entry.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cette recherche ne t'appartient pas.")
    if entry.match_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Aucun match créé pour cette recherche.")
    match = match_service.get_match_or_404(db, entry.match_id)
    return _to_match_response(db, match)
