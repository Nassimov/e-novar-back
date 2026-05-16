from __future__ import annotations

import math
import uuid
from datetime import datetime
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.booking import Booking, TutoringSession
from app.models.parent_link import ParentStudentLink
from app.models.profile import Profile
from app.models.session import Session as SessionModel, SessionStatus
from app.models.user import User
from app.schemas.session import (
    JoinSessionResponse,
    SessionListResponse,
    SessionRateRequest,
    SessionResponse,
)


class CancelSessionRequest(BaseModel):
    reason: Optional[str] = None


def _compute_refund_pct(scheduled_at: datetime) -> int:
    hours = (scheduled_at - datetime.utcnow()).total_seconds() / 3600
    if hours > 24:
        return 100
    if hours > 6:
        return 50
    return 0

router = APIRouter(tags=["sessions"])


@router.get("/", response_model=SessionListResponse)
async def list_sessions(
    status_filter: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List sessions for the current user."""
    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    role = current_user.get("role", "student")
    if role == "teacher":
        query = select(SessionModel).where(SessionModel.teacher_id == user.id)
    else:
        query = select(SessionModel).where(SessionModel.student_id == user.id)

    if status_filter:
        try:
            query = query.where(SessionModel.status == SessionStatus(status_filter))
        except ValueError:
            pass

    sessions = db.exec(query).all()
    total = len(sessions)
    offset = (page - 1) * size
    paginated = sessions[offset: offset + size]

    return SessionListResponse(
        items=[SessionResponse.model_validate(s) for s in paginated],
        total=total,
        page=page,
        size=size,
        pages=math.ceil(total / size) if total else 0,
    )


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get a specific session."""
    session = db.get(SessionModel, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user and session.student_id != user.id and session.teacher_id != user.id:
        if current_user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Access denied")

    return SessionResponse.model_validate(session)


@router.post("/{session_id}/cancel")
async def cancel_session(
    session_id: UUID,
    payload: Optional[CancelSessionRequest] = Body(None),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Cancel a session.

    Business rules:
    - Students with an accepted parent link cannot cancel (403).
    - Refund policy based on hours until session:
        >24h → 100% refund | 6–24h → 50% | <6h → 0%
    - Parents can cancel on behalf of linked children via
      POST /api/parent/sessions/{session_id}/cancel.
    - Admins can cancel any session.
    """
    uid = UUID(current_user["id"])
    role = current_user.get("role", "student")

    session = db.get(SessionModel, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    # Authorization
    is_student_owner = session.student_id == uid
    is_teacher_owner = session.teacher_id == uid
    if not is_student_owner and not is_teacher_owner and role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    # Students linked to a parent cannot self-cancel
    if is_student_owner and role == "student":
        parent_link = db.exec(
            select(ParentStudentLink)
            .where(ParentStudentLink.student_id == session.student_id)
            .where(ParentStudentLink.status == "accepted")
        ).first()
        if parent_link:
            raise HTTPException(
                status_code=403,
                detail="Cette séance est gérée par votre parent. Contactez-le pour l'annuler.",
            )

    if session.status in ("completed", "cancelled"):
        raise HTTPException(status_code=400, detail="Cannot cancel a completed or already cancelled session")

    now_utc = datetime.utcnow()
    refund_pct = _compute_refund_pct(session.scheduled_at)

    # Compute amounts from booking
    amount = 0
    if session.booking_id:
        booking = db.get(Booking, session.booking_id)
        if booking:
            amount = booking.amount

    refund_amount = int(amount * refund_pct / 100)
    teacher_payout = amount - refund_amount

    session.status = "cancelled"
    session.cancelled_by = uid
    session.cancelled_at = now_utc
    session.cancellation_reason = (payload.reason if payload else None)
    session.refund_percentage = refund_pct
    session.refund_amount = refund_amount
    session.teacher_payout_amount = teacher_payout
    db.add(session)
    db.commit()
    db.refresh(session)

    return {
        "id": str(session.id),
        "status": session.status,
        "refund_percentage": refund_pct,
        "refund_amount": refund_amount,
        "teacher_payout_amount": teacher_payout,
    }


@router.post("/{session_id}/rate", response_model=SessionResponse)
async def rate_session(
    session_id: UUID,
    payload: SessionRateRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Rate a completed session."""
    from datetime import datetime

    session = db.get(SessionModel, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.status != SessionStatus.completed:
        raise HTTPException(status_code=400, detail="Can only rate completed sessions")

    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None or session.student_id != user.id:
        raise HTTPException(status_code=403, detail="Only the student can rate this session")

    session.rating = payload.rating
    session.feedback = payload.feedback
    session.updated_at = datetime.utcnow()
    db.add(session)

    # Update teacher's average rating
    from app.models.teacher import TeacherProfile
    from sqlalchemy import func

    profile = db.exec(
        select(TeacherProfile).where(TeacherProfile.user_id == session.teacher_id)
    ).first()
    if profile:
        all_sessions = db.exec(
            select(SessionModel).where(
                SessionModel.teacher_id == session.teacher_id,
                SessionModel.rating.is_not(None),
            )
        ).all()
        ratings = [s.rating for s in all_sessions if s.rating is not None]
        if ratings:
            profile.rating = sum(ratings) / len(ratings)
            profile.reviews_count = len(ratings)
            db.add(profile)

    db.commit()
    db.refresh(session)
    return SessionResponse.model_validate(session)


@router.get("/{session_id}/replay")
async def get_session_replay(
    session_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get the replay URL for a session recording."""
    session = db.get(SessionModel, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user and session.student_id != user.id and session.teacher_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    if not session.replay_url:
        raise HTTPException(status_code=404, detail="No replay available for this session")

    return {"replay_url": session.replay_url}


@router.post("/{session_id}/join", response_model=JoinSessionResponse)
async def join_session(
    session_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get the video room details to join a live session."""
    from datetime import datetime

    session = db.get(SessionModel, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user and session.student_id != user.id and session.teacher_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    if session.status not in (SessionStatus.scheduled, SessionStatus.live):
        raise HTTPException(status_code=400, detail="Session is not joinable")

    # Create or reuse video room
    if not session.video_room_id:
        session.video_room_id = str(uuid.uuid4())
        session.status = SessionStatus.live
        session.updated_at = datetime.utcnow()
        db.add(session)
        db.commit()
        db.refresh(session)

    # Generate a join token (integrate with Daily.co / Jitsi / Whereby in production)
    join_token = str(uuid.uuid4())
    join_url = f"https://meet.enovar.dz/{session.video_room_id}?token={join_token}"

    return JoinSessionResponse(
        session_id=session.id,
        room_id=session.video_room_id,
        join_url=join_url,
        token=join_token,
    )


@router.get("/{session_id}/summary")
async def get_session_summary(
    session_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get or generate an AI summary for a session."""
    session = db.get(SessionModel, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user and session.student_id != user.id and session.teacher_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    if session.ai_summary:
        return {"summary": session.ai_summary, "generated": False}

    # Generate summary on demand
    from app.services.ai import generate_session_summary

    session_data = {
        "subject": session.subject,
        "level": session.level,
        "duration_minutes": session.duration_minutes,
        "notes": session.notes,
        "feedback": session.feedback,
        "rating": session.rating,
        "scheduled_at": session.scheduled_at.isoformat(),
    }
    summary = generate_session_summary(session_data)

    from datetime import datetime
    session.ai_summary = summary
    session.updated_at = datetime.utcnow()
    db.add(session)
    db.commit()

    return {"summary": summary, "generated": True}
