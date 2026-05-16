from __future__ import annotations

from datetime import datetime, timedelta, date
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.booking import TutoringSession
from app.models.catalog import Subject
from app.models.homework import Homework
from app.models.kp import KpBalance
from app.models.profile import Profile, StudentProfile, TeacherProfile

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


@router.get("/dashboard", response_model=StudentDashboardResponse)
async def student_dashboard(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])
    now_utc = datetime.utcnow()
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
    streak_days = _compute_streak(recent_sessions)

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
    budget_max = sp.budget_max if sp else None

    teacher_profs = db.exec(
        select(TeacherProfile)
        .where(TeacherProfile.status == "approved")
        .where(TeacherProfile.verified == True)
        .order_by(TeacherProfile.sponsored.desc(), TeacherProfile.rating_avg.desc(), TeacherProfile.students_count.desc())
        .limit(10)
    ).all()

    t_profile_ids = [tp.user_id for tp in teacher_profs]
    teacher_prof_map: dict[UUID, Profile] = {}
    if t_profile_ids:
        tpps = db.exec(select(Profile).where(Profile.id.in_(t_profile_ids))).all()
        teacher_prof_map = {p.id: p for p in tpps}

    recommended: list[DashboardTeacher] = []
    for tp in teacher_profs:
        p = teacher_prof_map.get(tp.user_id)
        if not p:
            continue
        if budget_max and tp.price_per_session > budget_max:
            continue
        recommended.append(DashboardTeacher(
            id=str(tp.user_id),
            name=p.full_name or "Enseignant",
            avatar_url=p.avatar_url,
            headline=tp.headline,
            price_per_session=tp.price_per_session,
            rating_avg=round(tp.rating_avg or 0.0, 1),
            kp_reward=tp.kp_reward,
            verified=tp.verified,
            sponsored=tp.sponsored,
        ))
        if len(recommended) >= 3:
            break

    # Budget-filter fallback
    if not recommended and teacher_profs:
        for tp in teacher_profs[:3]:
            p = teacher_prof_map.get(tp.user_id)
            if not p:
                continue
            recommended.append(DashboardTeacher(
                id=str(tp.user_id),
                name=p.full_name or "Enseignant",
                avatar_url=p.avatar_url,
                headline=tp.headline,
                price_per_session=tp.price_per_session,
                rating_avg=round(tp.rating_avg or 0.0, 1),
                kp_reward=tp.kp_reward,
                verified=tp.verified,
                sponsored=tp.sponsored,
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
        next_session_at=next_session_at,
        upcoming_sessions=upcoming_sessions,
        homework=homework_list,
        recommended_teachers=recommended,
    )
