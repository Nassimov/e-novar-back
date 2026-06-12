from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, func, select

from app.dependencies import get_admin_user, get_db
from app.models.enums import KpSource
from app.models.gamification import Challenge, ChallengeParticipation
from app.models.notification import Notification
from app.models.profile import Profile
from app.services.kp import award_kp

router = APIRouter(tags=["admin-challenges"])




class AdminCreateChallenge(BaseModel):
    id: Optional[str] = None
    title: str
    description: Optional[str] = None
    reward: int = 0
    audience: str = "student"
    proof_type: str = "image-or-pdf"
    active: bool = True
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    timer_duration_sec: Optional[int] = None
    rules: Optional[str] = None
    proof_instructions: Optional[str] = None
    approval_conditions: Optional[str] = None


class AdminUpdateChallenge(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    reward: Optional[int] = None
    audience: Optional[str] = None
    proof_type: Optional[str] = None
    active: Optional[bool] = None
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    timer_duration_sec: Optional[int] = None
    rules: Optional[str] = None
    proof_instructions: Optional[str] = None
    approval_conditions: Optional[str] = None


def _slugify(text: str) -> str:
    slug = text.lower().strip()
    slug = re.sub(r"[àâä]", "a", slug)
    slug = re.sub(r"[éèêë]", "e", slug)
    slug = re.sub(r"[îï]", "i", slug)
    slug = re.sub(r"[ôö]", "o", slug)
    slug = re.sub(r"[ùûü]", "u", slug)
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")[:60]


@router.get("/")
async def list_challenges(
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    challenges = db.exec(select(Challenge)).all()

    counts: Dict[str, int] = {}
    for row in db.exec(
        select(ChallengeParticipation.challenge_id, func.count(ChallengeParticipation.id))
        .group_by(ChallengeParticipation.challenge_id)
    ).all():
        counts[row[0]] = row[1]

    submitted_counts: Dict[str, int] = {}
    for row in db.exec(
        select(ChallengeParticipation.challenge_id, func.count(ChallengeParticipation.id))
        .where(ChallengeParticipation.status == "submitted")
        .group_by(ChallengeParticipation.challenge_id)
    ).all():
        submitted_counts[row[0]] = row[1]

    return {
        "challenges": [
            {
                "id": c.id,
                "title": c.title,
                "description": c.description,
                "reward": c.reward,
                "audience": c.audience,
                "proof_type": c.proof_type,
                "active": c.active,
                "ends_at": c.ends_at.isoformat() if c.ends_at else None,
                "timer_duration_sec": c.timer_duration_sec,
                "rules": c.rules,
                "proof_instructions": c.proof_instructions,
                "approval_conditions": c.approval_conditions,
                "participation_count": counts.get(c.id, 0),
                "pending_submissions": submitted_counts.get(c.id, 0),
            }
            for c in challenges
        ]
    }


@router.post("/", status_code=201)
async def create_challenge(
    payload: AdminCreateChallenge,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    challenge_id = payload.id or _slugify(payload.title)
    if db.get(Challenge, challenge_id):
        raise HTTPException(status_code=409, detail="Challenge with this id already exists")

    challenge = Challenge(
        id=challenge_id,
        title=payload.title,
        description=payload.description,
        reward=payload.reward,
        audience=payload.audience,
        proof_type=payload.proof_type,
        active=payload.active,
        starts_at=payload.starts_at,
        ends_at=payload.ends_at,
        timer_duration_sec=payload.timer_duration_sec,
        rules=payload.rules,
        proof_instructions=payload.proof_instructions,
        approval_conditions=payload.approval_conditions,
    )
    db.add(challenge)
    db.commit()
    db.refresh(challenge)
    return {"id": challenge.id, "title": challenge.title}


@router.put("/{challenge_id}")
async def update_challenge(
    challenge_id: str,
    payload: AdminUpdateChallenge,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    challenge = db.get(Challenge, challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail="Challenge not found")

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(challenge, field, value)

    db.add(challenge)
    db.commit()
    db.refresh(challenge)
    return {"id": challenge.id, "title": challenge.title, "active": challenge.active}


@router.delete("/{challenge_id}", status_code=204)
async def deactivate_challenge(
    challenge_id: str,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    challenge = db.get(Challenge, challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail="Challenge not found")
    challenge.active = False
    db.add(challenge)
    db.commit()
    return None


@router.get("/submissions")
async def list_submissions(
    status_filter: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    stmt = select(ChallengeParticipation).order_by(ChallengeParticipation.submitted_at.desc())
    if status_filter:
        stmt = stmt.where(ChallengeParticipation.status == status_filter)

    parts = db.exec(stmt).all()
    total = len(parts)
    offset = (page - 1) * size
    paginated = parts[offset: offset + size]

    challenge_ids = list({p.challenge_id for p in paginated})
    user_ids = list({p.user_id for p in paginated})

    challenges_map: Dict[str, Challenge] = {}
    if challenge_ids:
        for c in db.exec(select(Challenge).where(Challenge.id.in_(challenge_ids))).all():
            challenges_map[c.id] = c

    profiles_map: Dict[UUID, Profile] = {}
    if user_ids:
        for pr in db.exec(select(Profile).where(Profile.id.in_(user_ids))).all():
            profiles_map[pr.id] = pr

    items = []
    for p in paginated:
        c = challenges_map.get(p.challenge_id)
        pr = profiles_map.get(p.user_id)
        items.append({
            "id": str(p.id),
            "challenge_id": p.challenge_id,
            "challenge_title": c.title if c else "—",
            "user_id": str(p.user_id),
            "user_name": pr.full_name if pr else "—",
            "status": p.status,
            "proof_url": p.proof_url,
            "proof_name": p.proof_name,
            "reason": p.reason,
            "started_at": p.started_at.isoformat(),
            "submitted_at": p.submitted_at.isoformat() if p.submitted_at else None,
            "reviewed_at": p.reviewed_at.isoformat() if p.reviewed_at else None,
            "deadline_at": p.deadline_at.isoformat() if p.deadline_at else None,
            "reward": c.reward if c else 0,
        })

    return {
        "items": items,
        "total": total,
        "page": page,
        "size": size,
        "pages": max(1, -(-total // size)),
    }


@router.put("/submissions/{submission_id}/approve")
async def approve_submission(
    submission_id: UUID,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    part = db.get(ChallengeParticipation, submission_id)
    if part is None:
        raise HTTPException(status_code=404, detail="Submission not found")
    if part.status != "submitted":
        raise HTTPException(status_code=400, detail="Submission is not in submitted state")

    challenge = db.get(Challenge, part.challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail="Challenge not found")

    part.status = "approved"
    part.reviewed_by = UUID(current_user["id"]) if current_user.get("id") else None
    part.reviewed_at = datetime.utcnow()
    db.add(part)
    db.commit()

    award_kp(
        part.user_id,
        challenge.reward,
        KpSource.challenge,
        f"Défi complété : {challenge.title}",
        db,
    )

    notif = Notification(
        user_id=part.user_id,
        type="kp",
        title="Défi approuvé !",
        body=f"Ta soumission pour « {challenge.title} » a été approuvée. Tu gagnes {challenge.reward} EP !",
    )
    db.add(notif)
    db.commit()

    return {"message": "Submission approved", "ep_awarded": challenge.reward}


@router.put("/submissions/{submission_id}/reject")
async def reject_submission(
    submission_id: UUID,
    reason: str = Body("", embed=True),
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    part = db.get(ChallengeParticipation, submission_id)
    if part is None:
        raise HTTPException(status_code=404, detail="Submission not found")
    if part.status != "submitted":
        raise HTTPException(status_code=400, detail="Submission is not in submitted state")

    challenge = db.get(Challenge, part.challenge_id)

    part.status = "lost"
    part.reason = reason
    part.reviewed_by = UUID(current_user["id"]) if current_user.get("id") else None
    part.reviewed_at = datetime.utcnow()
    db.add(part)

    title_str = challenge.title if challenge else "défi"
    notif = Notification(
        user_id=part.user_id,
        type="system",
        title="Soumission refusée",
        body=f"Ta soumission pour « {title_str} » n'a pas été acceptée. Motif : {reason or 'Non conforme aux critères.'}",
    )
    db.add(notif)
    db.commit()

    return {"message": "Submission rejected"}
