from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
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
    # Admin-only override for who's actually at fault (see cancel_session) —
    # student/teacher self-cancellations always derive this from who's
    # calling, never from client input, so it can't be gamed.
    at_fault: Optional[str] = None  # "teacher" | "student"


def _compute_refund_pct(scheduled_at: datetime, at_fault: str) -> int:
    """
    at_fault="teacher" → always 100%: it's never the student's cost to bear
    when the teacher is the one cancelling a confirmed booking, however
    close to the session time.
    at_fault="student" → the time-based courtesy tiers (voluntary cancel).
    """
    if at_fault == "teacher":
        return 100
    hours = (scheduled_at - datetime.now(timezone.utc)).total_seconds() / 3600
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
def list_sessions(
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

    # A TutoringSession row is materialized (status="scheduled") the moment
    # the student books/pays — see student_teachers.py's booking-creation
    # flow — but the underlying Booking still starts out "pending" until the
    # teacher explicitly accepts it (accept_booking -> booking.status =
    # "confirmed"). Surfacing a still-pending one here (dashboards/session
    # lists on both sides) reads as "this session is on" when it may still
    # be refused, so filter those out — but only for the still-open
    # "scheduled"/"waiting"/"live" states; a "completed"/"cancelled"/
    # "no_show" session obviously already happened regardless of booking
    # status, so never exclude those. Sessions with no booking_id at all
    # (legacy/admin-created) are left as-is.
    pending_statuses = {"scheduled", "waiting", "live"}
    pending_ids = [s.id for s in sessions if s.status in pending_statuses and s.booking_id]
    if pending_ids:
        booking_ids = {s.booking_id for s in sessions if s.id in pending_ids}
        unconfirmed_booking_ids = {
            b.id for b in db.exec(
                select(Booking).where(Booking.id.in_(booking_ids), Booking.status != "confirmed")
            ).all()
        }
        if unconfirmed_booking_ids:
            sessions = [
                s for s in sessions
                if not (s.status in pending_statuses and s.booking_id in unconfirmed_booking_ids)
            ]

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
def get_session(
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
def cancel_session(
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

    now_utc = datetime.now(timezone.utc)

    # Who's actually at fault decides the refund policy — never the student's
    # cost to bear when the teacher backs out of an accepted lesson. Only an
    # admin (rare, manual) may override via payload.at_fault; a student or
    # teacher self-cancelling always gets their own role, never client input.
    if role == "teacher" and is_teacher_owner:
        at_fault = "teacher"
    elif role == "admin" and payload and payload.at_fault in ("teacher", "student"):
        at_fault = payload.at_fault
    else:
        at_fault = "student"

    refund_pct = _compute_refund_pct(session.scheduled_at, at_fault)

    # Compute this lesson's fair share of the booking (a pack shares ONE
    # Booking row across all its lessons — never refund/payout the whole
    # pack's price for a single cancelled lesson).
    amount = 0
    booking: Optional[Booking] = None
    if session.booking_id:
        booking = db.get(Booking, session.booking_id)
        if booking:
            amount = round(booking.amount / PACK_SIZES.get(booking.formula, 1))

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

    refund_result = {"refunded": False, "requires_manual_action": False, "method": None}
    if booking is not None and refund_amount > 0:
        from app.services.refunds import refund_amount_for_booking
        refund_result = refund_amount_for_booking(
            db, booking, refund_amount,
            note=f"Séance annulée par {'le professeur' if at_fault == 'teacher' else 'l’élève'} ({refund_pct}% remboursé).",
        )

    # A teacher backing out of an ALREADY-ACCEPTED, paid lesson is exactly as
    # disruptive as never responding in the first place — same escalating
    # strike/suspension counter (see app/workers/booking_tasks.py), just a
    # distinct reason string for admin visibility. Only counts once the
    # booking had actually been accepted (booking.status == "confirmed") —
    # cancelling before that point is what refuse_booking (with its own
    # strike) is for.
    if at_fault == "teacher" and booking is not None and booking.status == "confirmed":
        from app.services.booking_safety import apply_cancellation_side_effects, apply_teacher_strike
        apply_cancellation_side_effects(db, booking, reason="teacher_refused")
        apply_teacher_strike(
            db, session.teacher_id, "teacher_cancelled_confirmed",
            human_label="Tu as annulé une séance déjà confirmée.",
        )

    # Notify whichever party didn't take the cancelling action themselves —
    # they otherwise never hear about it. An admin-initiated cancellation
    # notifies both parties (neither one acted).
    if uid == session.student_id:
        notify_ids = [session.teacher_id]
    elif uid == session.teacher_id:
        notify_ids = [session.student_id]
    else:
        notify_ids = [session.student_id, session.teacher_id]
    from app.services.notification_engine import emit
    for notify_user_id in notify_ids:
        emit(
            db, event_type="lesson_cancelled", user_id=notify_user_id,
            context={"date": str(session.scheduled_at.date()), "reason": session.cancellation_reason or ""},
            data={"session_id": str(session.id)},
            dedup_key=f"lesson_cancelled:{session.id}:{notify_user_id}",
        )

    return {
        "id": str(session.id),
        "status": session.status,
        "refund_percentage": refund_pct,
        "refund_amount": refund_amount,
        "teacher_payout_amount": teacher_payout,
        "refunded": refund_result["refunded"],
        "refund_requires_manual_action": refund_result["requires_manual_action"],
    }


@router.post("/{session_id}/complete")
async def complete_session(
    session_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Low-level payout primitive — admin-only. Credits the teacher's payout
    for that one lesson.

    This used to let a teacher unilaterally mark their own session completed
    and instantly credit their own wallet — zero proof of attendance. That
    self-service path is gone: the real, student-attested completion flow now
    lives in app/routers/session_validation.py (token-based validation +
    trust score), which calls app.services.session_validation.
    credit_session_payout — the same underlying credit logic — once a session
    is legitimately validated or an admin approves it. This endpoint remains
    only as the admin override primitive (also used directly by
    admin/bookings.py's simulate_completion test tool).

    Payout is per-lesson, not per-booking: a pack booking's amount is split
    evenly across its PACK_SIZES[formula] sessions (see app.services.pricing),
    and each session credits its own share only once it is individually
    completed here. Re-completing an already-completed/cancelled session is
    rejected (400) — this is the idempotency guard against double-crediting.
    """
    role = current_user.get("role", "student")

    session = db.get(SessionModel, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    if role != "admin":
        raise HTTPException(status_code=403, detail="Only an admin can force-complete a session")

    if session.status in ("completed", "cancelled"):
        raise HTTPException(status_code=400, detail="Session is already completed or cancelled")

    booking = db.get(Booking, session.booking_id) if session.booking_id else None
    if booking is None:
        raise HTTPException(status_code=400, detail="Session has no associated booking")

    pack_size = PACK_SIZES.get(booking.formula, 1)
    payout = round(booking.amount / pack_size)

    session.status = "completed"
    session.ended_at = datetime.now(timezone.utc)
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
def get_session_replay(
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
def join_session(
    session_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Legacy/superseded — the real online-classroom join flow is now
    GET /api/classroom/{session_id}/room (app/routers/classroom.py), backed
    by real LiveKit rooms + short-lived signed tokens (app/services/
    livekit_video.py). This endpoint's `room_url`/`join_token` were always
    placeholders (a fake meet.enovar.dz URL + a random UUID, never a real
    signed credential) and nothing in the frontend calls it anymore — kept
    only for backward compatibility, do not build new features on it."""
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
            session.started_at = datetime.now(timezone.utc)
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
def get_session_summary(
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
