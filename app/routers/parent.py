from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db, require_role
from app.models.booking import Booking, TutoringSession
from app.models.catalog import Subject
from app.models.kp import KpBalance
from app.models.parent_link import ParentStudentLink
from app.models.profile import Profile, ParentProfile, StudentProfile
from app.routers.sessions import _compute_refund_pct

router = APIRouter(tags=["Parent"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class ParentChildSession(BaseModel):
    id: str
    teacher_id: str
    teacher_name: str
    teacher_avatar: Optional[str] = None
    subject: Optional[str] = None
    scheduled_at: str
    duration_min: Optional[int] = None
    mode: str
    status: str
    amount: int = 0
    room_url: Optional[str] = None


class ParentChild(BaseModel):
    student_id: str
    student_code: Optional[str] = None
    name: str
    first_name: Optional[str] = None
    avatar_url: Optional[str] = None
    level: Optional[str] = None
    kp_balance: int = 0
    kp_level: int = 1
    upcoming_sessions: List[ParentChildSession] = []
    next_session_at: Optional[str] = None
    sessions_today: int = 0


class ParentDashboardResponse(BaseModel):
    first_name: str
    full_name: str
    email: str
    avatar_url: Optional[str] = None
    total_children: int
    total_sessions_today: int
    children: List[ParentChild]


class CancelForChildRequest(BaseModel):
    reason: Optional[str] = None


# ── Helper ────────────────────────────────────────────────────────────────────

def _build_child_sessions(
    child_id: UUID,
    now_utc: datetime,
    db: Session,
    limit: int = 3,
) -> tuple[list[ParentChildSession], Optional[str], int]:
    """Return (upcoming_sessions, next_session_at_iso, sessions_today_count)."""
    today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)

    sessions = db.exec(
        select(TutoringSession)
        .where(TutoringSession.student_id == child_id)
        .where(TutoringSession.scheduled_at >= now_utc)
        .where(TutoringSession.status.notin_(["cancelled", "completed", "no_show"]))
        .order_by(TutoringSession.scheduled_at)
        .limit(limit)
    ).all()

    sessions_today = sum(1 for s in sessions if today_start <= s.scheduled_at < today_end)
    next_at = sessions[0].scheduled_at.isoformat() if sessions else None

    t_ids = list({s.teacher_id for s in sessions})
    teachers_map: dict[UUID, Profile] = {}
    if t_ids:
        tps = db.exec(select(Profile).where(Profile.id.in_(t_ids))).all()
        teachers_map = {p.id: p for p in tps}

    sub_ids = list({s.subject_id for s in sessions if s.subject_id})
    subjects_map: dict[UUID, str] = {}
    if sub_ids:
        subs = db.exec(select(Subject).where(Subject.id.in_(sub_ids))).all()
        subjects_map = {s.id: s.name for s in subs}

    book_ids = list({s.booking_id for s in sessions if s.booking_id})
    bookings_map: dict[UUID, Booking] = {}
    if book_ids:
        books = db.exec(select(Booking).where(Booking.id.in_(book_ids))).all()
        bookings_map = {b.id: b for b in books}

    result: list[ParentChildSession] = []
    for s in sessions:
        tp = teachers_map.get(s.teacher_id)
        booking = bookings_map.get(s.booking_id) if s.booking_id else None
        result.append(ParentChildSession(
            id=str(s.id),
            teacher_id=str(s.teacher_id),
            teacher_name=(tp.full_name or "Enseignant") if tp else "Enseignant",
            teacher_avatar=tp.avatar_url if tp else None,
            subject=subjects_map.get(s.subject_id) if s.subject_id else None,
            scheduled_at=s.scheduled_at.isoformat(),
            duration_min=s.duration_min or (booking.duration_min if booking else None),
            mode=s.mode,
            status=s.status,
            amount=booking.amount if booking else 0,
            room_url=s.room_url,
        ))
    return result, next_at, sessions_today


def _verify_child_link(parent_uid: UUID, student_id: UUID, db: Session) -> None:
    """Raise 403 if this student is not an accepted child of this parent."""
    link = db.exec(
        select(ParentStudentLink)
        .where(ParentStudentLink.parent_id == parent_uid)
        .where(ParentStudentLink.student_id == student_id)
        .where(ParentStudentLink.status == "accepted")
    ).first()
    if not link:
        raise HTTPException(status_code=403, detail="Cet enfant n'est pas lié à votre compte.")


# ── Dashboard ─────────────────────────────────────────────────────────────────

@router.get("/dashboard", response_model=ParentDashboardResponse)
async def parent_dashboard(
    current_user: Dict[str, Any] = Depends(require_role("parent")),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])
    now_utc = datetime.utcnow()

    profile = db.exec(select(Profile).where(Profile.id == uid)).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    # Accepted child links
    links = db.exec(
        select(ParentStudentLink)
        .where(ParentStudentLink.parent_id == uid)
        .where(ParentStudentLink.status == "accepted")
    ).all()

    children: list[ParentChild] = []
    total_today = 0

    for link in links:
        child_id = link.student_id
        child_profile = db.exec(select(Profile).where(Profile.id == child_id)).first()
        child_sp = db.exec(select(StudentProfile).where(StudentProfile.user_id == child_id)).first()
        child_kp = db.exec(select(KpBalance).where(KpBalance.user_id == child_id)).first()

        if not child_profile:
            continue

        child_sessions, next_at, today_count = _build_child_sessions(child_id, now_utc, db)
        total_today += today_count

        level_str = None
        if child_sp:
            parts = [child_sp.level_main, child_sp.level_detail, child_sp.speciality]
            level_str = " · ".join(p for p in parts if p) or None

        children.append(ParentChild(
            student_id=str(child_id),
            student_code=child_sp.student_code if child_sp else None,
            name=child_profile.full_name or child_profile.first_name or "Enfant",
            first_name=child_profile.first_name,
            avatar_url=child_profile.avatar_url,
            level=level_str,
            kp_balance=child_kp.balance if child_kp else 0,
            kp_level=child_kp.level if child_kp else 1,
            upcoming_sessions=child_sessions,
            next_session_at=next_at,
            sessions_today=today_count,
        ))

    first_name = profile.first_name or ""
    if not first_name and profile.full_name:
        parts = profile.full_name.strip().split()
        first_name = parts[0] if parts else ""
    if not first_name:
        first_name = "Parent"

    return ParentDashboardResponse(
        first_name=first_name,
        full_name=profile.full_name or "",
        email=profile.email or "",
        avatar_url=profile.avatar_url,
        total_children=len(children),
        total_sessions_today=total_today,
        children=children,
    )


# ── Children list ─────────────────────────────────────────────────────────────

@router.get("/children")
async def list_children(
    current_user: Dict[str, Any] = Depends(require_role("parent")),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])
    links = db.exec(
        select(ParentStudentLink)
        .where(ParentStudentLink.parent_id == uid)
        .where(ParentStudentLink.status == "accepted")
    ).all()

    children = []
    for link in links:
        p = db.exec(select(Profile).where(Profile.id == link.student_id)).first()
        sp = db.exec(select(StudentProfile).where(StudentProfile.user_id == link.student_id)).first()
        if p:
            children.append({
                "student_id": str(link.student_id),
                "student_code": sp.student_code if sp else None,
                "name": p.full_name or "Enfant",
                "avatar_url": p.avatar_url,
                "linked_at": link.accepted_at.isoformat() if link.accepted_at else None,
            })
    return {"children": children}


# ── Child sessions ────────────────────────────────────────────────────────────

@router.get("/children/{child_id}/sessions")
async def get_child_sessions(
    child_id: UUID,
    type: str = Query("upcoming", pattern="^(upcoming|past)$"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    current_user: Dict[str, Any] = Depends(require_role("parent")),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])
    _verify_child_link(uid, child_id, db)

    now_utc = datetime.utcnow()
    if type == "upcoming":
        query = (
            select(TutoringSession)
            .where(TutoringSession.student_id == child_id)
            .where(TutoringSession.scheduled_at >= now_utc)
            .where(TutoringSession.status.notin_(["cancelled", "completed", "no_show"]))
            .order_by(TutoringSession.scheduled_at)
        )
    else:
        query = (
            select(TutoringSession)
            .where(TutoringSession.student_id == child_id)
            .where(TutoringSession.status.in_(["completed", "cancelled", "no_show"]))
            .order_by(TutoringSession.scheduled_at.desc())
        )

    all_s = db.exec(query).all()
    total = len(all_s)
    offset = (page - 1) * size
    page_s = all_s[offset: offset + size]

    t_ids = list({s.teacher_id for s in page_s})
    teachers_map: dict[UUID, Profile] = {}
    if t_ids:
        tps = db.exec(select(Profile).where(Profile.id.in_(t_ids))).all()
        teachers_map = {p.id: p for p in tps}

    sub_ids = list({s.subject_id for s in page_s if s.subject_id})
    subjects_map: dict[UUID, str] = {}
    if sub_ids:
        subs = db.exec(select(Subject).where(Subject.id.in_(sub_ids))).all()
        subjects_map = {s.id: s.name for s in subs}

    book_ids = list({s.booking_id for s in page_s if s.booking_id})
    bookings_map: dict[UUID, Booking] = {}
    if book_ids:
        books = db.exec(select(Booking).where(Booking.id.in_(book_ids))).all()
        bookings_map = {b.id: b for b in books}

    items = []
    for s in page_s:
        tp = teachers_map.get(s.teacher_id)
        booking = bookings_map.get(s.booking_id) if s.booking_id else None
        hrs = (s.scheduled_at - now_utc).total_seconds() / 3600 if type == "upcoming" else -1
        refund_preview = "full" if hrs > 24 else ("partial" if hrs > 6 else "none")
        items.append({
            "id": str(s.id),
            "teacher_id": str(s.teacher_id),
            "teacher_name": (tp.full_name or "Enseignant") if tp else "Enseignant",
            "teacher_avatar": tp.avatar_url if tp else None,
            "subject": subjects_map.get(s.subject_id) if s.subject_id else None,
            "scheduled_at": s.scheduled_at.isoformat(),
            "duration_min": s.duration_min or (booking.duration_min if booking else None),
            "mode": s.mode,
            "status": s.status,
            "amount": booking.amount if booking else 0,
            "room_url": s.room_url,
            "notes_teacher": s.notes_teacher,
            "cancellation_reason": s.cancellation_reason,
            "can_cancel": s.status in ("scheduled", "live", "waiting"),
            "cancel_refund": refund_preview,
        })

    return {
        "items": items,
        "total": total,
        "page": page,
        "pages": math.ceil(total / size) if total else 0,
    }


# ── Parent cancels session for a child ───────────────────────────────────────

@router.post("/sessions/{session_id}/cancel")
async def parent_cancel_session(
    session_id: UUID,
    payload: Optional[CancelForChildRequest] = Body(None),
    current_user: Dict[str, Any] = Depends(require_role("parent")),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])

    session = db.get(TutoringSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    # Verify the student is this parent's linked child
    _verify_child_link(uid, session.student_id, db)

    if session.status in ("completed", "cancelled"):
        raise HTTPException(status_code=400, detail="Cannot cancel a completed or already cancelled session")

    now_utc = datetime.utcnow()
    refund_pct = _compute_refund_pct(session.scheduled_at)

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

    return {
        "id": str(session.id),
        "status": "cancelled",
        "refund_percentage": refund_pct,
        "refund_amount": refund_amount,
        "teacher_payout_amount": teacher_payout,
    }


# ── Child progress ────────────────────────────────────────────────────────────

@router.get("/children/{child_id}/progress")
async def get_child_progress(
    child_id: UUID,
    current_user: Dict[str, Any] = Depends(require_role("parent")),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])
    _verify_child_link(uid, child_id, db)

    child_profile = db.exec(select(Profile).where(Profile.id == child_id)).first()
    if not child_profile:
        raise HTTPException(status_code=404, detail="Child not found")

    child_sp = db.exec(select(StudentProfile).where(StudentProfile.user_id == child_id)).first()
    child_kp = db.exec(select(KpBalance).where(KpBalance.user_id == child_id)).first()

    completed_sessions = db.exec(
        select(TutoringSession)
        .where(TutoringSession.student_id == child_id)
        .where(TutoringSession.status == "completed")
    ).all()

    from app.models.homework import Homework
    homeworks = db.exec(select(Homework).where(Homework.student_id == child_id)).all()

    level_str = None
    if child_sp:
        parts = [child_sp.level_main, child_sp.level_detail, child_sp.speciality]
        level_str = " · ".join(p for p in parts if p) or None

    return {
        "student_id": str(child_id),
        "name": child_profile.full_name or "Enfant",
        "first_name": child_profile.first_name,
        "avatar_url": child_profile.avatar_url,
        "student_code": child_sp.student_code if child_sp else None,
        "level": level_str,
        "kp_balance": child_kp.balance if child_kp else 0,
        "kp_level": child_kp.level if child_kp else 1,
        "kp_week_earned": child_kp.week_earned if child_kp else 0,
        "total_sessions_completed": len(completed_sessions),
        "homework_total": len(homeworks),
        "homework_done": sum(1 for h in homeworks if h.status in ("graded", "submitted")),
    }


# ── Parent payments (kept for backward compat) ────────────────────────────────

@router.get("/payments")
async def get_parent_payments(
    page: int = 1,
    size: int = 20,
    current_user: Dict[str, Any] = Depends(require_role("parent")),
    db: Session = Depends(get_db),
):
    return {"items": [], "total": 0, "page": page, "size": size, "pages": 0}
