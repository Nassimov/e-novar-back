from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.gamification import Challenge, ChallengeParticipation

router = APIRouter(tags=["challenges"])


class ParticipationOut(BaseModel):
    id: str
    status: str
    started_at: str
    deadline_at: Optional[str]
    submitted_at: Optional[str]
    proof_url: Optional[str]
    proof_name: Optional[str]
    reason: Optional[str]


class ChallengeOut(BaseModel):
    id: str
    title: str
    description: str
    reward: int
    audience: str
    proof_type: str
    active: bool
    ends_at: Optional[str]
    timer_duration_sec: Optional[int]
    rules: Optional[str]
    proof_instructions: Optional[str]
    approval_conditions: Optional[str]
    participation: Optional[ParticipationOut]


class ChallengesListResponse(BaseModel):
    challenges: List[ChallengeOut]
    active_count: int
    potential_ep: int


class HistoryOut(BaseModel):
    participations: List[dict]


def _participation_out(p: ChallengeParticipation) -> ParticipationOut:
    return ParticipationOut(
        id=str(p.id),
        status=p.status,
        started_at=p.started_at.isoformat(),
        deadline_at=p.deadline_at.isoformat() if p.deadline_at else None,
        submitted_at=p.submitted_at.isoformat() if p.submitted_at else None,
        proof_url=p.proof_url,
        proof_name=p.proof_name,
        reason=p.reason,
    )


def _challenge_out(c: Challenge, p: Optional[ChallengeParticipation]) -> ChallengeOut:
    return ChallengeOut(
        id=c.id,
        title=c.title,
        description=c.description or "",
        reward=c.reward,
        audience=c.audience,
        proof_type=c.proof_type or "image-or-pdf",
        active=c.active,
        ends_at=c.ends_at.isoformat() if c.ends_at else None,
        timer_duration_sec=c.timer_duration_sec,
        rules=c.rules,
        proof_instructions=c.proof_instructions,
        approval_conditions=c.approval_conditions,
        participation=_participation_out(p) if p else None,
    )


def _auto_expire(user_id: UUID, db: Session) -> None:
    now = datetime.utcnow()
    expired_parts = db.exec(
        select(ChallengeParticipation).where(
            ChallengeParticipation.user_id == user_id,
            ChallengeParticipation.status == "in_progress",
            ChallengeParticipation.deadline_at.isnot(None),
            ChallengeParticipation.deadline_at < now,
        )
    ).all()
    if expired_parts:
        for ep in expired_parts:
            ep.status = "expired"
            db.add(ep)
        db.commit()


@router.get("/", response_model=ChallengesListResponse)
async def list_challenges(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    role = current_user["role"]
    now = datetime.utcnow()

    _auto_expire(user_id, db)

    stmt = select(Challenge).where(Challenge.active == True)
    stmt = stmt.where(
        (Challenge.ends_at.is_(None)) | (Challenge.ends_at >= now)
    )
    if role == "student":
        stmt = stmt.where(Challenge.audience.in_(["student", "both"]))
    elif role == "teacher":
        stmt = stmt.where(Challenge.audience.in_(["teacher", "both"]))

    challenges = db.exec(stmt).all()

    participations = {
        p.challenge_id: p
        for p in db.exec(
            select(ChallengeParticipation).where(
                ChallengeParticipation.user_id == user_id
            )
        ).all()
    }

    result = [_challenge_out(c, participations.get(c.id)) for c in challenges]

    active_count = sum(
        1 for r in result
        if r.participation and r.participation.status == "in_progress"
    )
    potential_ep = sum(
        c.reward
        for c, r in zip(challenges, result)
        if r.participation and r.participation.status in ("in_progress", "submitted")
    )

    return ChallengesListResponse(
        challenges=result,
        active_count=active_count,
        potential_ep=potential_ep,
    )


@router.get("/history/", response_model=HistoryOut)
async def get_history(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])

    parts = db.exec(
        select(ChallengeParticipation)
        .where(ChallengeParticipation.user_id == user_id)
        .order_by(ChallengeParticipation.started_at.desc())
    ).all()

    challenge_ids = list({p.challenge_id for p in parts})
    challenges_map: Dict[str, Challenge] = {}
    if challenge_ids:
        for c in db.exec(select(Challenge).where(Challenge.id.in_(challenge_ids))).all():
            challenges_map[c.id] = c

    items = []
    for p in parts:
        c = challenges_map.get(p.challenge_id)
        items.append({
            "participation_id": str(p.id),
            "challenge_id": p.challenge_id,
            "challenge_title": c.title if c else "—",
            "challenge_description": c.description if c else "",
            "reward": c.reward if c else 0,
            "status": p.status,
            "started_at": p.started_at.isoformat(),
            "submitted_at": p.submitted_at.isoformat() if p.submitted_at else None,
            "proof_url": p.proof_url,
            "proof_name": p.proof_name,
            "reason": p.reason,
            "deadline_at": p.deadline_at.isoformat() if p.deadline_at else None,
        })

    return HistoryOut(participations=items)


@router.post("/{challenge_id}/start", response_model=ChallengeOut)
async def start_challenge(
    challenge_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])

    challenge = db.get(Challenge, challenge_id)
    if challenge is None or not challenge.active:
        raise HTTPException(status_code=404, detail="Challenge not found or inactive")

    existing = db.exec(
        select(ChallengeParticipation).where(
            ChallengeParticipation.challenge_id == challenge_id,
            ChallengeParticipation.user_id == user_id,
        )
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Already participated in this challenge")

    now = datetime.utcnow()
    deadline = (
        now + timedelta(seconds=challenge.timer_duration_sec)
        if challenge.timer_duration_sec
        else None
    )

    part = ChallengeParticipation(
        challenge_id=challenge_id,
        user_id=user_id,
        status="in_progress",
        started_at=now,
        deadline_at=deadline,
    )
    db.add(part)
    db.commit()
    db.refresh(part)

    return _challenge_out(challenge, part)


@router.post("/{challenge_id}/decline", status_code=status.HTTP_204_NO_CONTENT)
async def decline_challenge(
    challenge_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])

    challenge = db.get(Challenge, challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail="Challenge not found")

    existing = db.exec(
        select(ChallengeParticipation).where(
            ChallengeParticipation.challenge_id == challenge_id,
            ChallengeParticipation.user_id == user_id,
        )
    ).first()

    if existing:
        if existing.status != "in_progress":
            raise HTTPException(status_code=400, detail="Cannot decline a challenge not in progress")
        existing.status = "declined"
        db.add(existing)
    else:
        part = ChallengeParticipation(
            challenge_id=challenge_id,
            user_id=user_id,
            status="declined",
        )
        db.add(part)

    db.commit()
    return None


@router.post("/{challenge_id}/submit")
async def submit_proof(
    challenge_id: str,
    proof_file: Optional[UploadFile] = File(None),
    proof_text: Optional[str] = Form(None),
    proof_url_link: Optional[str] = Form(None),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    now = datetime.utcnow()

    part = db.exec(
        select(ChallengeParticipation).where(
            ChallengeParticipation.challenge_id == challenge_id,
            ChallengeParticipation.user_id == user_id,
        )
    ).first()

    if part is None:
        raise HTTPException(status_code=404, detail="Participation not found")
    if part.status != "in_progress":
        raise HTTPException(status_code=400, detail="Challenge is not in progress")
    if part.deadline_at and part.deadline_at < now:
        part.status = "expired"
        db.add(part)
        db.commit()
        raise HTTPException(status_code=400, detail="Challenge deadline has passed")

    if proof_file:
        from app.services.storage import upload_file
        content = await proof_file.read()
        url = upload_file(
            content,
            proof_file.filename or "proof",
            proof_file.content_type,
            "challenge-proofs",
        )
        part.proof_url = url
        part.proof_name = proof_file.filename
    elif proof_text:
        part.proof_url = f"text::{proof_text}"
        part.proof_name = "text-proof"
    elif proof_url_link:
        part.proof_url = proof_url_link
        part.proof_name = "url-proof"
    else:
        raise HTTPException(status_code=422, detail="No proof provided")

    part.status = "submitted"
    part.submitted_at = now
    db.add(part)
    db.commit()
    db.refresh(part)

    return _participation_out(part)
