from __future__ import annotations

import json
import random
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.parent_link import ParentStudentLink
from app.models.profile import ParentProfile, Profile, StudentProfile
from app.models.teacher import TeacherProfile
from app.models.user import User

router = APIRouter(tags=["onboarding"])


# ─────────────────────────── helpers ─────────────────────────────────────────

def _generate_student_code(db: Session) -> str:
    for _ in range(15):
        code = f"KRN-2025-{random.randint(1000, 9999)}"
        if not db.exec(select(StudentProfile).where(StudentProfile.student_code == code)).first():
            return code
    return f"KRN-2025-{random.randint(10000, 99999)}"


def _generate_parent_link_code(db: Session) -> str:
    for _ in range(15):
        code = f"KRN-L-{random.randint(1000, 9999)}"
        if not db.exec(select(StudentProfile).where(StudentProfile.parent_link_code == code)).first():
            return code
    return f"KRN-L-{random.randint(10000, 99999)}"


def _generate_parent_code(db: Session) -> str:
    for _ in range(15):
        code = f"KRN-P-{random.randint(1000, 9999)}"
        if not db.exec(select(ParentProfile).where(ParentProfile.parent_code == code)).first():
            return code
    return f"KRN-P-{random.randint(10000, 99999)}"


# ─────────────────────────── schemas ─────────────────────────────────────────

class StudentOnboardingRequest(BaseModel):
    # Profile fields
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None
    birth_date: Optional[str] = None   # ISO date "YYYY-MM-DD"
    gender: Optional[str] = None
    avatar_url: Optional[str] = None
    wilaya: Optional[str] = None
    # StudentProfile fields
    student_code: Optional[str] = None  # client-proposed (honoured if unique)
    monitoring_mode: Optional[str] = None
    level_main: Optional[str] = None
    level_detail: Optional[str] = None
    speciality: Optional[str] = None
    subjects_interested: Optional[List[str]] = None
    goals: Optional[List[str]] = None
    online_only: Optional[bool] = None
    budget_tier: Optional[str] = None
    budget_min: Optional[int] = None
    budget_max: Optional[int] = None
    # Parent linking — student enters the parent's KRN-P-XXXX code
    parent_code: Optional[str] = None


class OnboardingCompleteRequest(BaseModel):
    full_name: Optional[str] = None
    wilaya: Optional[str] = None
    phone: Optional[str] = None
    bio: Optional[str] = None
    subjects: Optional[List[str]] = None
    levels: Optional[List[str]] = None
    price_per_session: Optional[int] = None
    modes: Optional[List[str]] = None
    experience_years: Optional[int] = None
    child_name: Optional[str] = None
    child_level: Optional[str] = None


class OnboardingStatusResponse(BaseModel):
    completed: bool
    role: str
    missing_fields: List[str]


# ─────────────────────────── student complete ─────────────────────────────────

@router.post("/student/complete")
async def complete_student_onboarding(
    payload: StudentOnboardingRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Save all student onboarding data in one call.
    Updates Profile + creates/updates StudentProfile.
    Optionally links to a parent via their KRN-P-XXXX code.
    Sends a welcome email (non-fatal).
    Returns { student_code, parent_linked }.
    """
    uid = UUID(current_user["id"])

    # ── Profile ───────────────────────────────────────────────────────────────
    profile = db.exec(select(Profile).where(Profile.id == uid)).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")

    if payload.first_name:
        profile.first_name = payload.first_name
    if payload.last_name:
        profile.last_name = payload.last_name
    if payload.phone:
        profile.phone = payload.phone
    if payload.gender:
        profile.gender = payload.gender
    if payload.avatar_url:
        profile.avatar_url = payload.avatar_url
    if payload.wilaya:
        profile.wilaya = payload.wilaya
    if payload.birth_date:
        try:
            profile.birth_date = date.fromisoformat(payload.birth_date)
        except ValueError:
            pass
    profile.onboarding_completed = True
    profile.updated_at = datetime.utcnow()
    db.add(profile)

    # ── StudentProfile ────────────────────────────────────────────────────────
    sp = db.exec(select(StudentProfile).where(StudentProfile.user_id == uid)).first()
    if sp is None:
        sp = StudentProfile(user_id=uid)

    # Honour proposed student_code only if unique (not taken by another user)
    if not sp.student_code:
        proposed = (payload.student_code or "").strip().upper()
        if proposed:
            clash = db.exec(
                select(StudentProfile).where(StudentProfile.student_code == proposed)
            ).first()
            sp.student_code = proposed if (not clash or clash.user_id == uid) else _generate_student_code(db)
        else:
            sp.student_code = _generate_student_code(db)

    if not sp.parent_link_code:
        sp.parent_link_code = _generate_parent_link_code(db)

    if payload.monitoring_mode:
        sp.monitoring_mode = payload.monitoring_mode
    if payload.level_main:
        sp.level_main = payload.level_main
    if payload.level_detail:
        sp.level_detail = payload.level_detail
    if payload.speciality:
        sp.speciality = payload.speciality
    if payload.subjects_interested is not None:
        sp.subjects_interested = payload.subjects_interested
    if payload.goals is not None:
        sp.goals = payload.goals
    if payload.online_only is not None:
        sp.online_only = payload.online_only
    if payload.budget_tier:
        sp.budget_tier = payload.budget_tier
    if payload.budget_min is not None:
        sp.budget_min = payload.budget_min
    if payload.budget_max is not None:
        sp.budget_max = payload.budget_max

    db.add(sp)

    # ── Parent link ───────────────────────────────────────────────────────────
    parent_linked = False
    if payload.parent_code:
        code = payload.parent_code.strip().upper()
        parent_prof = db.exec(
            select(ParentProfile).where(ParentProfile.parent_code == code)
        ).first()
        if parent_prof:
            existing = db.exec(
                select(ParentStudentLink)
                .where(ParentStudentLink.parent_id == parent_prof.user_id)
                .where(ParentStudentLink.student_id == uid)
            ).first()
            if not existing:
                db.add(ParentStudentLink(
                    parent_id=parent_prof.user_id,
                    student_id=uid,
                    status="accepted",
                ))
            parent_linked = True

    try:
        db.commit()
        db.refresh(sp)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Onboarding save failed: {exc}")

    # Welcome email (non-fatal)
    try:
        from app.workers.email_tasks import send_welcome_email
        send_welcome_email.delay(profile.email or "", profile.full_name or "")
    except Exception:
        pass

    return {"student_code": sp.student_code, "parent_linked": parent_linked}


# ─────────────────────────── find parent by code ─────────────────────────────

@router.get("/student/find-parent/{code}")
async def find_parent_by_code(
    code: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Resolve a KRN-P-XXXX parent code.
    Returns { parent_id, name } if found, 404 otherwise.
    """
    parent_prof = db.exec(
        select(ParentProfile).where(ParentProfile.parent_code == code.strip().upper())
    ).first()
    if not parent_prof:
        raise HTTPException(status_code=404, detail="Aucun parent trouvé avec ce code.")

    profile = db.exec(select(Profile).where(Profile.id == parent_prof.user_id)).first()
    name = (profile.full_name or "Parent").strip() if profile else "Parent"
    return {"parent_id": str(parent_prof.user_id), "name": name}


# ─────────────────────────── legacy endpoints ────────────────────────────────

@router.post("/complete")
async def complete_onboarding(
    payload: OnboardingCompleteRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Legacy teacher/parent onboarding endpoint."""
    statement = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(statement).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    if payload.full_name:
        user.full_name = payload.full_name
    if payload.wilaya:
        user.wilaya = payload.wilaya
    if payload.phone:
        user.phone = payload.phone
    if payload.bio:
        user.bio = payload.bio

    user.onboarding_completed = True
    user.updated_at = datetime.utcnow()
    db.add(user)

    if user.role.value == "teacher" and any([payload.subjects, payload.levels, payload.price_per_session]):
        stmt = select(TeacherProfile).where(TeacherProfile.user_id == user.id)
        tp = db.exec(stmt).first()
        if tp is None:
            tp = TeacherProfile(user_id=user.id)
        if payload.subjects:
            tp.subjects = json.dumps(payload.subjects)
        if payload.levels:
            tp.levels = json.dumps(payload.levels)
        if payload.price_per_session is not None:
            tp.price_per_session = payload.price_per_session
        if payload.modes:
            tp.modes = json.dumps(payload.modes)
        if payload.experience_years is not None:
            tp.experience_years = payload.experience_years
        tp.bio = payload.bio or tp.bio
        tp.updated_at = datetime.utcnow()
        db.add(tp)

    db.commit()
    return {"message": "Onboarding completed", "role": user.role.value}


@router.get("/status", response_model=OnboardingStatusResponse)
async def get_onboarding_status(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    statement = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(statement).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    missing = []
    if not user.full_name:
        missing.append("full_name")
    if not user.wilaya:
        missing.append("wilaya")
    if not user.phone:
        missing.append("phone")

    if user.role.value == "teacher":
        stmt = select(TeacherProfile).where(TeacherProfile.user_id == user.id)
        tp = db.exec(stmt).first()
        if tp is None or not tp.subjects or tp.subjects == "[]":
            missing.append("subjects")
        if tp is None or not tp.price_per_session:
            missing.append("price_per_session")

    return OnboardingStatusResponse(
        completed=user.onboarding_completed,
        role=user.role.value,
        missing_fields=missing,
    )
