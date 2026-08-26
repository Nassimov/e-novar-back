from __future__ import annotations

import math
from datetime import datetime, timedelta, date, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.booking import Booking, TutoringSession
from app.models.catalog import Level, Subject
from app.models.homework import Homework
from app.models.kp import KpBalance
from app.models.parent_link import ParentStudentLink
from app.models.profile import Profile, StudentProfile, TeacherProfile
from app.services import recommendation
from app.services.boost import is_boost_active

router = APIRouter(tags=["Student"])


class DashboardSession(BaseModel):
    id: str
    teacher_id: str
    teacher_name: str
    teacher_avatar: Optional[str] = None
    subject: Optional[str] = None
    scheduled_at: str
    mode: str
    status: str


class DashboardHomework(BaseModel):
    id: str
    title: str
    subject: Optional[str] = None
    due_at: Optional[str] = None
    due_label: Optional[str] = None
    status: str


class DashboardTeacher(BaseModel):
    id: str
    name: str
    avatar_url: Optional[str] = None
    headline: Optional[str] = None
    price_per_session: int
    currency: str = "DZD"
    rating_avg: float
    kp_reward: int
    verified: bool
    sponsored: bool


class StudentDashboardResponse(BaseModel):
    first_name: str
    full_name: str
    email: str
    avatar_url: Optional[str] = None
    kp_balance: int
    kp_week_earned: int
    kp_level: int
    streak_days: int
    sessions_today: int
    sessions_completed_total: int
    hours_completed_total: float
    next_session_at: Optional[str] = None
    upcoming_sessions: List[DashboardSession]
    homework: List[DashboardHomework]
    recommended_teachers: List[DashboardTeacher]


def _compute_streak(sessions: list) -> int:
    today = datetime.utcnow().date()
    active: set[date] = set()
    for s in sessions:
        if s.scheduled_at:
            try:
                active.add(s.scheduled_at.date())
            except Exception:
                pass
    streak = 0
    check = today
    while check in active:
        streak += 1
        check -= timedelta(days=1)
    return streak


def _compute_streak_with_shield(sessions: list, user_id, db) -> int:
    """Compute streak and consume an active streak_shield if it would save the streak."""
    from uuid import UUID as _UUID
    today = datetime.utcnow().date()
    yesterday = today - timedelta(days=1)
    two_days_ago = today - timedelta(days=2)

    active: set[date] = set()
    for s in sessions:
        if s.scheduled_at:
            try:
                active.add(s.scheduled_at.date())
            except Exception:
                pass

    # Shield fires when: yesterday is missing AND two_days_ago has activity
    # (meaning: there is an existing streak to protect, and yesterday broke it)
    if yesterday not in active and two_days_ago in active:
        try:
            uid = _UUID(str(user_id)) if not isinstance(user_id, _UUID) else user_id
            from app.services.effects import get_active_streak_shield, consume_streak_shield
            shield = get_active_streak_shield(uid, db)
            if shield:
                consume_streak_shield(shield, db)
                db.commit()
                active = active | {yesterday}
        except Exception:
            pass

    streak = 0
    check = today
    while check in active:
        streak += 1
        check -= timedelta(days=1)
    return streak


@router.get("/dashboard", response_model=StudentDashboardResponse)
async def student_dashboard(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])
    now_utc = datetime.now(timezone.utc)
    today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)

    # ── Profile ──────────────────────────────────────────────────────────────────
    profile = db.exec(select(Profile).where(Profile.id == uid)).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    sp = db.exec(select(StudentProfile).where(StudentProfile.user_id == uid)).first()

    # ── KP ────────────────────────────────────────────────────────────────────────
    kp = db.exec(select(KpBalance).where(KpBalance.user_id == uid)).first()

    # ── Upcoming sessions ─────────────────────────────────────────────────────────
    upcoming_raw = db.exec(
        select(TutoringSession)
        .where(TutoringSession.student_id == uid)
        .where(TutoringSession.scheduled_at >= now_utc)
        .where(TutoringSession.status.in_(["scheduled", "live", "waiting"]))
        .order_by(TutoringSession.scheduled_at)
        .limit(20)
    ).all()

    sessions_today = sum(
        1 for s in upcoming_raw
        if today_start <= s.scheduled_at < today_end
    )
    next_session_at = upcoming_raw[0].scheduled_at.isoformat() if upcoming_raw else None

    # ── Streak ────────────────────────────────────────────────────────────────────
    recent_sessions = db.exec(
        select(TutoringSession)
        .where(TutoringSession.student_id == uid)
        .where(TutoringSession.scheduled_at >= now_utc - timedelta(days=90))
        .where(TutoringSession.status.in_(["completed", "scheduled", "live"]))
    ).all()
    streak_days = _compute_streak_with_shield(recent_sessions, uid, db)

    # ── Lifetime totals (for the profile page stats cards) ─────────────────────
    completed_sessions = db.exec(
        select(TutoringSession).where(
            TutoringSession.student_id == uid,
            TutoringSession.status == "completed",
        )
    ).all()
    sessions_completed_total = len(completed_sessions)
    hours_completed_total = round(
        sum((s.duration_min or 0) for s in completed_sessions) / 60.0, 1
    )

    # ── Teacher names for upcoming sessions ───────────────────────────────────────
    teacher_ids = list({s.teacher_id for s in upcoming_raw[:8]})
    teachers_map: dict[UUID, Profile] = {}
    if teacher_ids:
        tps = db.exec(select(Profile).where(Profile.id.in_(teacher_ids))).all()
        teachers_map = {p.id: p for p in tps}

    # ── Subject names for upcoming sessions ───────────────────────────────────────
    subject_ids = list({s.subject_id for s in upcoming_raw[:8] if s.subject_id})
    subjects_map: dict[UUID, str] = {}
    if subject_ids:
        subs = db.exec(select(Subject).where(Subject.id.in_(subject_ids))).all()
        subjects_map = {s.id: s.name for s in subs}

    upcoming_sessions: list[DashboardSession] = []
    for s in upcoming_raw[:8]:
        tp = teachers_map.get(s.teacher_id)
        upcoming_sessions.append(DashboardSession(
            id=str(s.id),
            teacher_id=str(s.teacher_id),
            teacher_name=(tp.full_name or "Enseignant") if tp else "Enseignant",
            teacher_avatar=tp.avatar_url if tp else None,
            subject=subjects_map.get(s.subject_id) if s.subject_id else None,
            scheduled_at=s.scheduled_at.isoformat(),
            mode=s.mode,
            status=s.status,
        ))

    # ── Homework ──────────────────────────────────────────────────────────────────
    hw_raw = db.exec(
        select(Homework)
        .where(Homework.student_id == uid)
        .where(Homework.status.in_(["todo", "submitted"]))
        .order_by(Homework.due_at)
        .limit(5)
    ).all()

    homework_list: list[DashboardHomework] = []
    for h in hw_raw:
        status_str = h.status if isinstance(h.status, str) else h.status.value
        homework_list.append(DashboardHomework(
            id=str(h.id),
            title=h.title,
            due_at=h.due_at.isoformat() if h.due_at else None,
            due_label=h.due_label,
            status=status_str,
        ))

    # ── Recommended teachers ──────────────────────────────────────────────────────
    # Ranked by best-overall-compatibility (subjects, goals, recurring availability,
    # budget, lesson format, wilaya/online, spoken languages) via app.services.recommendation
    # — shared scoring logic, not a re-implementation. A teacher is never excluded
    # solely for lacking declared schedule slots; only quality/verification gate eligibility.
    ranked = recommendation.rank_teachers(db, uid, limit=3) if sp else []

    t_profile_ids = [tp.user_id for tp in ranked]
    teacher_prof_map: dict[UUID, Profile] = {}
    if t_profile_ids:
        tpps = db.exec(select(Profile).where(Profile.id.in_(t_profile_ids))).all()
        teacher_prof_map = {p.id: p for p in tpps}
    min_price_map = recommendation.min_price_for_teachers(db, t_profile_ids)

    recommended: list[DashboardTeacher] = []
    for tp in ranked:
        p = teacher_prof_map.get(tp.user_id)
        if not p:
            continue
        recommended.append(DashboardTeacher(
            id=str(tp.user_id),
            name=p.full_name or "Enseignant",
            avatar_url=p.avatar_url,
            headline=tp.headline,
            price_per_session=min_price_map.get(tp.user_id) or tp.price_per_session,
            currency=tp.currency,
            rating_avg=round(tp.rating_avg or 0.0, 1),
            kp_reward=tp.kp_reward,
            verified=tp.verified,
            sponsored=is_boost_active(tp),
        ))

    first_name = profile.first_name or ""
    if not first_name and profile.full_name:
        parts = profile.full_name.strip().split()
        first_name = parts[0] if parts else ""
    if not first_name:
        first_name = "là"

    return StudentDashboardResponse(
        first_name=first_name,
        full_name=profile.full_name or "",
        email=profile.email or "",
        avatar_url=profile.avatar_url,
        kp_balance=kp.balance if kp else 0,
        kp_week_earned=kp.week_earned if kp else 0,
        kp_level=kp.level if kp else 1,
        streak_days=streak_days,
        sessions_today=sessions_today,
        sessions_completed_total=sessions_completed_total,
        hours_completed_total=hours_completed_total,
        next_session_at=next_session_at,
        upcoming_sessions=upcoming_sessions,
        homework=homework_list,
        recommended_teachers=recommended,
    )


# ─── Student sessions list ────────────────────────────────────────────────────

class SessionListItem(BaseModel):
    id: str
    teacher_id: str
    teacher_name: str
    teacher_avatar: Optional[str] = None
    subject: Optional[str] = None
    level: Optional[str] = None
    scheduled_at: str
    duration_min: Optional[int] = None
    mode: str
    status: str
    amount: int = 0
    currency: str = "DZD"
    room_url: Optional[str] = None
    notes_teacher: Optional[str] = None
    summary: Optional[str] = None
    cancellation_reason: Optional[str] = None
    cancelled_at: Optional[str] = None
    refund_percentage: Optional[int] = None
    can_cancel: bool = False
    cancel_refund: str = "none"
    # Booking.status — 'pending' means the teacher hasn't accepted/refused
    # yet (or, for cash/transfer/RIB, an admin hasn't confirmed the manual
    # payment yet). The TutoringSession row already exists at this point
    # (status='scheduled') but the booking isn't actually confirmed —
    # frontend uses this to show the session in a separate "en attente de
    # confirmation" tab instead of mixing it in with real upcoming sessions.
    booking_status: Optional[str] = None
    booking_created_at: Optional[str] = None
    payment_method: Optional[str] = None


class StudentSessionListResponse(BaseModel):
    items: List[SessionListItem]
    total: int
    page: int
    pages: int
    parent_linked: bool


@router.get("/sessions", response_model=StudentSessionListResponse)
async def student_session_list(
    type: str = Query("upcoming", pattern="^(upcoming|past)$"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])
    now_utc = datetime.now(timezone.utc)

    # Check parent link
    parent_link = db.exec(
        select(ParentStudentLink)
        .where(ParentStudentLink.student_id == uid)
        .where(ParentStudentLink.status == "accepted")
    ).first()
    parent_linked = parent_link is not None

    if type == "upcoming":
        query = (
            select(TutoringSession)
            .where(TutoringSession.student_id == uid)
            .where(TutoringSession.scheduled_at >= now_utc)
            .where(TutoringSession.status.notin_(["cancelled", "completed", "no_show"]))
            .order_by(TutoringSession.scheduled_at)
        )
    else:
        query = (
            select(TutoringSession)
            .where(TutoringSession.student_id == uid)
            .where(TutoringSession.status.in_(["completed", "cancelled", "no_show"]))
            .order_by(TutoringSession.scheduled_at.desc())
        )

    all_sessions = db.exec(query).all()
    total = len(all_sessions)
    offset = (page - 1) * size
    page_sessions = all_sessions[offset: offset + size]

    # Batch load related entities
    t_ids = list({s.teacher_id for s in page_sessions})
    teachers_map: dict[UUID, Profile] = {}
    if t_ids:
        tps = db.exec(select(Profile).where(Profile.id.in_(t_ids))).all()
        teachers_map = {p.id: p for p in tps}

    sub_ids = list({s.subject_id for s in page_sessions if s.subject_id})
    subjects_map: dict[UUID, str] = {}
    if sub_ids:
        subs = db.exec(select(Subject).where(Subject.id.in_(sub_ids))).all()
        subjects_map = {s.id: s.name for s in subs}

    lvl_ids = list({s.level_id for s in page_sessions if s.level_id})
    levels_map: dict[UUID, str] = {}
    if lvl_ids:
        lvls = db.exec(select(Level).where(Level.id.in_(lvl_ids))).all()
        levels_map = {l.id: l.label for l in lvls}

    book_ids = list({s.booking_id for s in page_sessions if s.booking_id})
    bookings_map: dict[UUID, Booking] = {}
    if book_ids:
        books = db.exec(select(Booking).where(Booking.id.in_(book_ids))).all()
        bookings_map = {b.id: b for b in books}

    items: list[SessionListItem] = []
    for s in page_sessions:
        tp = teachers_map.get(s.teacher_id)
        booking = bookings_map.get(s.booking_id) if s.booking_id else None
        amount = booking.amount if booking else 0
        currency = booking.currency if booking else "DZD"
        duration = s.duration_min or (booking.duration_min if booking else None)

        can_cancel = False
        cancel_refund = "none"
        if s.status in ("scheduled", "live", "waiting") and not parent_linked:
            can_cancel = True
            hrs = (s.scheduled_at - now_utc).total_seconds() / 3600
            cancel_refund = "full" if hrs > 24 else ("partial" if hrs > 6 else "none")

        items.append(SessionListItem(
            id=str(s.id),
            teacher_id=str(s.teacher_id),
            teacher_name=(tp.full_name or "Enseignant") if tp else "Enseignant",
            teacher_avatar=tp.avatar_url if tp else None,
            subject=subjects_map.get(s.subject_id) if s.subject_id else None,
            level=levels_map.get(s.level_id) if s.level_id else None,
            scheduled_at=s.scheduled_at.isoformat(),
            duration_min=duration,
            mode=s.mode,
            status=s.status,
            amount=amount,
            currency=currency,
            room_url=s.room_url,
            notes_teacher=s.notes_teacher,
            summary=s.summary,
            cancellation_reason=s.cancellation_reason,
            cancelled_at=s.cancelled_at.isoformat() if s.cancelled_at else None,
            refund_percentage=s.refund_percentage,
            can_cancel=can_cancel,
            cancel_refund=cancel_refund,
            booking_status=booking.status if booking else None,
            booking_created_at=booking.created_at.isoformat() if booking else None,
            payment_method=booking.payment_method if booking else None,
        ))

    return StudentSessionListResponse(
        items=items,
        total=total,
        page=page,
        pages=math.ceil(total / size) if total else 0,
        parent_linked=parent_linked,
    )
