from __future__ import annotations

import math
import uuid
from datetime import datetime
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.booking import Booking, TutoringSession
from app.models.catalog import Level, Subject
from app.models.parent_link import ParentStudentLink
from app.models.profile import Profile, TeacherProfile
from app.models.session import Session as SessionModel, SessionStatus
from app.schemas.session import (
    JoinSessionResponse,
    SessionListResponse,
    SessionResponse,
)
from app.services.pricing import PACK_SIZES


class CancelSessionRequest(BaseModel):
    reason: Optional[str] = None


def _compute_refund_pct(scheduled_at: datetime) -> int:
    hours = (scheduled_at - datetime.utcnow()).total_seconds() / 3600
    if hours > 24:
        return 100
    if hours > 6:
        return 50
    return 0


def _to_response(db: Session, session: SessionModel) -> SessionResponse:
    """Build a SessionResponse from a real TutoringSession row, resolving
    subject/level names via a join since the model only stores IDs."""
    subject_name = None
    level_name = None
    if session.subject_id:
        subj = db.get(Subject, session.subject_id)
        subject_name = subj.name if subj else None
    if session.level_id:
        lvl = db.get(Level, session.level_id)
        level_name = lvl.label if lvl else None

    return SessionResponse(
        id=session.id,
        booking_id=session.booking_id,
        teacher_id=session.teacher_id,
        student_id=session.student_id,
        subject_id=session.subject_id,
        subject_name=subject_name,
        level_id=session.level_id,
        level_name=level_name,
        scheduled_at=session.scheduled_at,
        started_at=session.started_at,
        ended_at=session.ended_at,
        duration_min=session.duration_min,
        mode=session.mode,
        status=session.status,
        room_url=session.room_url,
        replay_url=session.replay_url,
        summary=session.summary,
        notes_teacher=session.notes_teacher,
        teacher_payout_amount=session.teacher_payout_amount,
        no_show=session.no_show,
        created_at=session.created_at,
    )


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
    user = db.get(Profile, UUID(current_user["id"]))
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

    query = query.order_by(SessionModel.scheduled_at.desc())

    sessions = db.exec(query).all()
    total = len(sessions)
    offset = (page - 1) * size
    paginated = sessions[offset: offset + size]

    return SessionListResponse(
        items=[_to_response(db, s) for s in paginated],
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

    user = db.get(Profile, UUID(current_user["id"]))
    if user and session.student_id != user.id and session.teacher_id != user.id:
        if current_user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Access denied")

    return _to_response(db, session)


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


@router.post("/{session_id}/complete")
async def complete_session(
    session_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mark a session completed and credit the teacher's payout for that lesson.

    Payout is per-lesson, not per-booking: a pack booking's amount is split
    evenly across its PACK_SIZES[formula] sessions (see app.services.pricing),
    and each session credits its own share only once it is individually
    completed here. Re-completing an already-completed/cancelled session is
    rejected (400) — this is the idempotency guard against double-crediting.
    """
    uid = UUID(current_user["id"])
    role = current_user.get("role", "student")

    session = db.get(SessionModel, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.teacher_id != uid and role != "admin":
        raise HTTPException(status_code=403, detail="Only the session's teacher can mark it completed")

    if session.status in ("completed", "cancelled"):
        raise HTTPException(status_code=400, detail="Session is already completed or cancelled")

    booking = db.get(Booking, session.booking_id) if session.booking_id else None
    if booking is None:
        raise HTTPException(status_code=400, detail="Session has no associated booking")

    pack_size = PACK_SIZES.get(booking.formula, 1)
    payout = round(booking.amount / pack_size)

    session.status = "completed"
    session.ended_at = datetime.utcnow()
    session.teacher_payout_amount = payout
    db.add(session)

    teacher_profile = db.get(TeacherProfile, session.teacher_id)
    if teacher_profile is not None:
        teacher_profile.wallet_balance_dzd += payout
        db.add(teacher_profile)

    db.commit()
    db.refresh(session)

    return {
        "id": str(session.id),
        "status": session.status,
        "teacher_payout_amount": payout,
        "wallet_balance_dzd": teacher_profile.wallet_balance_dzd if teacher_profile else None,
    }


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

    user = db.get(Profile, UUID(current_user["id"]))
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
    session = db.get(SessionModel, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    user = db.get(Profile, UUID(current_user["id"]))
    if user and session.student_id != user.id and session.teacher_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    if session.status not in (SessionStatus.scheduled, SessionStatus.live):
        raise HTTPException(status_code=400, detail="Session is not joinable")

    # Create or reuse video room (real field is room_url, not video_room_id)
    if not session.room_url:
        room_id = str(uuid.uuid4())
        session.room_url = f"https://meet.enovar.dz/{room_id}"
        session.status = SessionStatus.live
        if session.started_at is None:
            session.started_at = datetime.utcnow()
        db.add(session)
        db.commit()
        db.refresh(session)

    room_id = session.room_url.rsplit("/", 1)[-1]
    # Generate a join token (integrate with Daily.co / Jitsi / Whereby in production)
    join_token = str(uuid.uuid4())
    join_url = f"{session.room_url}?token={join_token}"

    return JoinSessionResponse(
        session_id=session.id,
        room_id=room_id,
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

    user = db.get(Profile, UUID(current_user["id"]))
    if user and session.student_id != user.id and session.teacher_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    if session.summary:
        return {"summary": session.summary, "generated": False}

    # Generate summary on demand
    from app.services.ai import generate_session_summary

    subject_name = None
    if session.subject_id:
        subj = db.get(Subject, session.subject_id)
        subject_name = subj.name if subj else None
    level_name = None
    if session.level_id:
        lvl = db.get(Level, session.level_id)
        level_name = lvl.label if lvl else None

    session_data = {
        "subject": subject_name,
        "level": level_name,
        "duration_minutes": session.duration_min,
        "notes": session.notes_teacher,
        "scheduled_at": session.scheduled_at.isoformat(),
    }
    summary = generate_session_summary(session_data)

    session.summary = summary
    db.add(session)
    db.commit()

    return {"summary": summary, "generated": True}
