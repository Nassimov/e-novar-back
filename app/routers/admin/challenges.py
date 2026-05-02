from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from app.dependencies import get_db, require_role
from app.models.challenge import ChallengeDef, ChallengeSubmission, ChallengeSubmissionStatus
from app.models.kp import KpSource
from app.models.user import User
from app.services.kp import award_kp

router = APIRouter(tags=["admin-challenges"])

admin_required = require_role("admin")


@router.get("/submissions")
async def list_challenge_submissions(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status_filter: str = Query(None, alias="status"),
    current_user: Dict[str, Any] = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """List all challenge submissions for review."""
    query = select(ChallengeSubmission)
    if status_filter:
        try:
            query = query.where(ChallengeSubmission.status == ChallengeSubmissionStatus(status_filter))
        except ValueError:
            pass

    submissions = db.exec(query.order_by(ChallengeSubmission.submitted_at.desc())).all()
    total = len(submissions)
    offset = (page - 1) * size
    paginated = submissions[offset: offset + size]

    result = []
    for sub in paginated:
        challenge = db.get(ChallengeDef, sub.challenge_id)
        user = db.get(User, sub.user_id)
        result.append({
            "id": str(sub.id),
            "challenge_id": str(sub.challenge_id),
            "challenge_title": challenge.title if challenge else "Unknown",
            "user_id": str(sub.user_id),
            "user_name": user.full_name if user else "Unknown",
            "proof_url": sub.proof_url,
            "proof_text": sub.proof_text,
            "status": sub.status.value,
            "reason": sub.reason,
            "submitted_at": sub.submitted_at.isoformat(),
            "reward_kp": challenge.reward_kp if challenge else 0,
        })

    return {
        "items": result,
        "total": total,
        "page": page,
        "size": size,
        "pages": math.ceil(total / size) if total else 0,
    }


@router.put("/submissions/{submission_id}/approve")
async def approve_submission(
    submission_id: UUID,
    current_user: Dict[str, Any] = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Approve a challenge submission and award KP."""
    submission = db.get(ChallengeSubmission, submission_id)
    if submission is None:
        raise HTTPException(status_code=404, detail="Submission not found")

    if submission.status != ChallengeSubmissionStatus.pending:
        raise HTTPException(status_code=400, detail="Submission is not pending")

    challenge = db.get(ChallengeDef, submission.challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail="Challenge not found")

    submission.status = ChallengeSubmissionStatus.approved
    submission.reviewed_at = datetime.utcnow()
    db.add(submission)
    db.commit()

    # Award KP to user
    award_kp(
        submission.user_id,
        challenge.reward_kp,
        KpSource.challenge,
        f"Challenge complété: {challenge.title}",
        db,
    )

    # Send notification
    from app.models.notification import Notification
    notif = Notification(
        user_id=submission.user_id,
        title="Challenge approuvé !",
        body=f"Votre soumission pour '{challenge.title}' a été approuvée. Vous gagnez {challenge.reward_kp} KP !",
        type="kp",
    )
    db.add(notif)
    db.commit()

    return {"message": "Submission approved", "kp_awarded": challenge.reward_kp}


@router.put("/submissions/{submission_id}/decline")
async def decline_submission(
    submission_id: UUID,
    reason: str = "",
    current_user: Dict[str, Any] = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Decline a challenge submission."""
    submission = db.get(ChallengeSubmission, submission_id)
    if submission is None:
        raise HTTPException(status_code=404, detail="Submission not found")

    submission.status = ChallengeSubmissionStatus.declined
    submission.reason = reason
    submission.reviewed_at = datetime.utcnow()
    db.add(submission)

    challenge = db.get(ChallengeDef, submission.challenge_id)

    # Notify user
    from app.models.notification import Notification
    title = challenge.title if challenge else "challenge"
    notif = Notification(
        user_id=submission.user_id,
        title="Soumission refusée",
        body=f"Votre soumission pour '{title}' n'a pas été approuvée. Raison: {reason or 'Non conforme aux critères.'}",
        type="system",
    )
    db.add(notif)
    db.commit()

    return {"message": "Submission declined"}


@router.get("/rewards/pending")
async def list_pending_rewards(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    current_user: Dict[str, Any] = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """List pending KP reward redemptions."""
    # In a full implementation this would query a RewardRedemption table
    return {"items": [], "total": 0, "message": "Reward redemption table not yet implemented"}


@router.put("/rewards/{reward_id}/approve")
async def approve_reward(
    reward_id: UUID,
    current_user: Dict[str, Any] = Depends(admin_required),
):
    """Approve a KP reward redemption."""
    return {"message": "Reward approved", "reward_id": str(reward_id)}


@router.put("/rewards/{reward_id}/decline")
async def decline_reward(
    reward_id: UUID,
    reason: str = "",
    current_user: Dict[str, Any] = Depends(admin_required),
):
    """Decline a KP reward redemption."""
    return {"message": "Reward declined", "reward_id": str(reward_id), "reason": reason}
