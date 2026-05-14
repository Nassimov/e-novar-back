from __future__ import annotations

import base64
import logging
import random
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.parent_link import ParentStudentLink
from app.models.profile import ParentProfile, Profile, StudentProfile

logger = logging.getLogger(__name__)
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


def _upload_base64_avatar(data_url: str, user_id: str) -> Optional[str]:
    """Decode a base64 data URL and upload to Supabase Storage. Returns public URL or None."""
    try:
        match = re.match(r"data:([^;]+);base64,(.+)", data_url, re.DOTALL)
        if not match:
            return None
        content_type = match.group(1)
        file_bytes = base64.b64decode(match.group(2))
        ext = content_type.split("/")[-1].replace("jpeg", "jpg") if "/" in content_type else "jpg"
        from app.services.storage import upload_file
        return upload_file(
            file_bytes=file_bytes,
            filename=f"avatar.{ext}",
            content_type=content_type,
            folder=f"avatars/{user_id}",
        )
    except Exception as exc:
        logger.warning("Base64 avatar upload failed for %s: %s", user_id, exc)
        return None


def _award_welcome_kp(user_id: UUID, db: Session, amount: int = 30) -> None:
    """Insert KP transaction. The apply_kp_transaction Supabase trigger updates kp_balances."""
    try:
        from app.models.kp import KpTransaction
        tx = KpTransaction(
            user_id=user_id,
            amount=amount,
            source="bonus",
            label="Bienvenue sur E-NOVAR ! 🎉",
            ref_type="onboarding",
        )
        db.add(tx)
        db.commit()
    except Exception as exc:
        logger.warning("Welcome KP bonus failed for %s: %s", user_id, exc)
        db.rollback()


def _insert_user_role(uid: UUID, db: Session) -> None:
    """Ensure (user_id, student) row exists in user_roles."""
    db.execute(
        text(
            "INSERT INTO public.user_roles (user_id, role) "
            "VALUES (:uid, 'student') "
            "ON CONFLICT (user_id, role) DO NOTHING"
        ),
        {"uid": str(uid)},
    )


def _insert_student_subjects(uid: UUID, subjects: List[str], db: Session) -> None:
    """Populate student_subjects table from the names list."""
    # Clear previous entries so a re-submit replaces them cleanly
    db.execute(
        text("DELETE FROM public.student_subjects WHERE student_id = :sid"),
        {"sid": str(uid)},
    )
    for priority, name in enumerate(subjects, start=1):
        row = db.execute(
            text("SELECT id FROM public.subjects WHERE name = :name LIMIT 1"),
            {"name": name},
        ).fetchone()
        if row:
            db.execute(
                text(
                    "INSERT INTO public.student_subjects (student_id, subject_id, priority) "
                    "VALUES (:sid, :subj_id, :pri) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"sid": str(uid), "subj_id": str(row[0]), "pri": priority},
            )


def _insert_student_goals(uid: UUID, goals: List[str], db: Session) -> None:
    """Populate student_goals table from the labels list."""
    # Clear previous entries so a re-submit replaces them cleanly
    db.execute(
        text("DELETE FROM public.student_goals WHERE student_id = :sid"),
        {"sid": str(uid)},
    )
    for label in goals:
        row = db.execute(
            text("SELECT id FROM public.goal_definitions WHERE label = :label LIMIT 1"),
            {"label": label},
        ).fetchone()
        if row:
            db.execute(
                text(
                    "INSERT INTO public.student_goals (student_id, goal_id) "
                    "VALUES (:sid, :gid) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"sid": str(uid), "gid": row[0]},
            )


# ─────────────────────────── schemas ─────────────────────────────────────────

class StudentOnboardingRequest(BaseModel):
    # Profile fields
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None
    birth_date: Optional[str] = None   # ISO "YYYY-MM-DD"
    gender: Optional[str] = None
    avatar_url: Optional[str] = None   # Supabase Storage URL (NOT base64 — upload separately)
    wilaya: Optional[str] = None
    # StudentProfile fields
    student_code: Optional[str] = None
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
    available_days: Optional[List[int]] = None
    available_slots: Optional[List[str]] = None
    parent_code: Optional[str] = None


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
    Save ALL student onboarding data in one atomic call (called from the Welcome page).

    Writes to:
      - public.profiles           (name, phone, birth_date, gender, avatar_url, wilaya)
      - public.student_profiles   (level, subjects, goals, budget, monitoring, availability)
      - public.user_roles         (role = 'student')
      - public.student_subjects   (normalised subject links)
      - public.student_goals      (normalised goal links)
      - public.parent_student_links (if parent_code provided)
      - public.kp_transactions    (+30 EP welcome bonus → trigger updates kp_balances)

    NOTE: avatar must be pre-uploaded via POST /api/profile/avatar.
    If avatar_url starts with 'data:' it will be uploaded here as a fallback.

    Returns { student_code, parent_linked, avatar_url }.
    """
    uid = UUID(current_user["id"])

    # ── 1. Resolve avatar URL ─────────────────────────────────────────────────
    # Prefer pre-uploaded URL. Fallback: upload inline base64 (should rarely happen).
    avatar_url = payload.avatar_url
    if avatar_url and avatar_url.startswith("data:"):
        logger.warning("Received base64 avatar for %s — uploading inline (slow path)", uid)
        uploaded = _upload_base64_avatar(avatar_url, str(uid))
        avatar_url = uploaded  # None = upload failed → skip field

    # ── 2. Profile ────────────────────────────────────────────────────────────
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
    if avatar_url:
        profile.avatar_url = avatar_url
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

    # ── 3. StudentProfile ─────────────────────────────────────────────────────
    sp = db.exec(select(StudentProfile).where(StudentProfile.user_id == uid)).first()
    if sp is None:
        sp = StudentProfile(user_id=uid)

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
    if payload.available_days is not None:
        sp.available_days = payload.available_days
    if payload.available_slots is not None:
        sp.available_slots = payload.available_slots

    db.add(sp)

    # ── 4. user_roles (role = student) ───────────────────────────────────────
    _insert_user_role(uid, db)

    # ── 5. Parent link ────────────────────────────────────────────────────────
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

    # ── 6. Main commit ────────────────────────────────────────────────────────
    try:
        db.commit()
        db.refresh(sp)
    except Exception as exc:
        db.rollback()
        logger.error("Onboarding commit failed for %s: %s", uid, exc)
        raise HTTPException(status_code=500, detail=f"Onboarding save failed: {exc}")

    # ── 7. student_subjects + student_goals (separate transaction) ────────────
    try:
        if sp.subjects_interested:
            _insert_student_subjects(uid, sp.subjects_interested, db)
        if sp.goals:
            _insert_student_goals(uid, sp.goals, db)
        db.commit()
    except Exception as exc:
        logger.warning("student_subjects/goals insert failed for %s: %s", uid, exc)
        db.rollback()

    # ── 8. Welcome KP bonus (+30 EP) ─────────────────────────────────────────
    _award_welcome_kp(uid, db, amount=30)

    # ── 9. Welcome email (non-fatal) ──────────────────────────────────────────
    try:
        from app.workers.email_tasks import send_welcome_email
        send_welcome_email.delay(profile.email or "", profile.full_name or "")
    except Exception:
        pass

    logger.info(
        "Student onboarding complete: uid=%s code=%s subjects=%s goals=%s",
        uid, sp.student_code,
        sp.subjects_interested or [],
        sp.goals or [],
    )

    return {
        "student_code": sp.student_code,
        "parent_linked": parent_linked,
        "avatar_url": profile.avatar_url,
    }


# ─────────────────────────── find parent by code ─────────────────────────────

@router.get("/student/find-parent/{code}")
async def find_parent_by_code(
    code: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    parent_prof = db.exec(
        select(ParentProfile).where(ParentProfile.parent_code == code.strip().upper())
    ).first()
    if not parent_prof:
        raise HTTPException(status_code=404, detail="Aucun parent trouvé avec ce code.")
    profile = db.exec(select(Profile).where(Profile.id == parent_prof.user_id)).first()
    name = (profile.full_name or "Parent").strip() if profile else "Parent"
    return {"parent_id": str(parent_prof.user_id), "name": name}


# ─────────────────────────── status ──────────────────────────────────────────

@router.get("/status", response_model=OnboardingStatusResponse)
async def get_onboarding_status(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])
    profile = db.exec(select(Profile).where(Profile.id == uid)).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")

    role = current_user["role"]
    missing: List[str] = []

    if not profile.first_name:
        missing.append("first_name")
    if not profile.wilaya:
        missing.append("wilaya")
    if not profile.phone:
        missing.append("phone")

    if role == "student":
        sp = db.exec(select(StudentProfile).where(StudentProfile.user_id == uid)).first()
        if sp is None or not sp.level_main:
            missing.append("level_main")
        if sp is None or not sp.subjects_interested:
            missing.append("subjects_interested")

    return OnboardingStatusResponse(
        completed=profile.onboarding_completed,
        role=role,
        missing_fields=missing,
    )


# ─────────────────────────── legacy no-op ────────────────────────────────────

@router.post("/complete")
async def complete_onboarding_legacy(
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    role = current_user.get("role", "student")
    return {
        "message": "This endpoint is deprecated. Use /api/onboarding/student/complete.",
        "redirect": f"/api/onboarding/{role}/complete",
    }
