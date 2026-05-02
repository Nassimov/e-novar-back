from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.challenge import ChallengeDef, ChallengeSubmission, ChallengeSubmissionStatus
from app.models.user import User
from app.schemas.challenge import ChallengeSubmitRequest, ChallengeSubmissionResponse
from app.schemas.kp import ChallengeResponse

router = APIRouter(tags=["challenges"])


@router.get("/", response_model=List[ChallengeResponse])
async def list_challenges(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List active challenges."""
    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    challenges = db.exec(
        select(ChallengeDef).where(ChallengeDef.is_active == True)
    ).all()

    # Get user's submissions
    user_submissions = {
        str(sub.challenge_id): sub
        for sub in db.exec(
            select(ChallengeSubmission).where(ChallengeSubmission.user_id == user.id)
        ).all()
    }

    total = len(challenges)
    offset = (page - 1) * size
    paginated = challenges[offset: offset + size]

    result = []
    for c in paginated:
        sub = user_submissions.get(str(c.id))
        result.append(ChallengeResponse(
            id=c.id,
            title=c.title,
            description=c.description,
            reward_kp=c.reward_kp,
            proof_type=c.proof_type.value,
            audience=c.audience.value,
            is_active=c.is_active,
            ends_at=c.ends_at,
            user_submitted=sub is not None,
            user_status=sub.status.value if sub else None,
        ))
    return result


@router.get("/{challenge_id}", response_model=ChallengeResponse)
async def get_challenge(
    challenge_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get a specific challenge."""
    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()

    challenge = db.get(ChallengeDef, challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail="Challenge not found")

    sub = None
    if user:
        sub = db.exec(
            select(ChallengeSubmission).where(
                ChallengeSubmission.challenge_id == challenge_id,
                ChallengeSubmission.user_id == user.id,
            )
        ).first()

    return ChallengeResponse(
        id=challenge.id,
        title=challenge.title,
        description=challenge.description,
        reward_kp=challenge.reward_kp,
        proof_type=challenge.proof_type.value,
        audience=challenge.audience.value,
        is_active=challenge.is_active,
        ends_at=challenge.ends_at,
        user_submitted=sub is not None,
        user_status=sub.status.value if sub else None,
    )


@router.post("/{challenge_id}/submit", response_model=ChallengeSubmissionResponse, status_code=status.HTTP_201_CREATED)
async def submit_challenge(
    challenge_id: UUID,
    payload: ChallengeSubmitRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Submit a challenge completion."""
    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    challenge = db.get(ChallengeDef, challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail="Challenge not found")
    if not challenge.is_active:
        raise HTTPException(status_code=400, detail="Challenge is no longer active")
    if challenge.ends_at and challenge.ends_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Challenge has ended")

    # Check for existing submission
    existing = db.exec(
        select(ChallengeSubmission).where(
            ChallengeSubmission.challenge_id == challenge_id,
            ChallengeSubmission.user_id == user.id,
        )
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Already submitted this challenge")

    submission = ChallengeSubmission(
        challenge_id=challenge_id,
        user_id=user.id,
        proof_url=payload.proof_url,
        proof_text=payload.proof_text,
    )
    db.add(submission)
    db.commit()
    db.refresh(submission)
    return ChallengeSubmissionResponse.model_validate(submission)


@router.get("/submissions", response_model=List[ChallengeSubmissionResponse])
async def list_my_submissions(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List the current user's challenge submissions."""
    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    submissions = db.exec(
        select(ChallengeSubmission).where(ChallengeSubmission.user_id == user.id)
    ).all()
    return [ChallengeSubmissionResponse.model_validate(s) for s in submissions]


@router.delete("/submissions/{submission_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_submission(
    submission_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a pending challenge submission."""
    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()

    submission = db.get(ChallengeSubmission, submission_id)
    if submission is None:
        raise HTTPException(status_code=404, detail="Submission not found")

    if user and submission.user_id != user.id and current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    if submission.status != ChallengeSubmissionStatus.pending:
        raise HTTPException(status_code=400, detail="Can only delete pending submissions")

    db.delete(submission)
    db.commit()
    return None
