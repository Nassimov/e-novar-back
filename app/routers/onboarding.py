from __future__ import annotations

import base64
import json
import logging
import random
import re
import unicodedata
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import text
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.catalog import TeacherDiploma, TeacherMode, TeacherSubjectPrice
from app.models.parent_link import ParentStudentLink
from app.models.profile import ParentProfile, Profile, StudentProfile, TeacherProfile

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


def _slugify(text: str) -> str:
    """Convert text to a DB-safe slug: remove accents, lowercase, replace non-alphanum with _."""
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def _upsert_subject(name: str, db: Session) -> str:
    """Get or create a subject by name (case-insensitive). Returns subject UUID as str."""
    row = db.execute(
        text("SELECT id FROM public.subjects WHERE LOWER(name) = LOWER(:name) LIMIT 1"),
        {"name": name},
    ).fetchone()
    if row:
        return str(row[0])
    slug = _slugify(name)
    # Resolve slug conflicts
    base = slug
    for i in range(1, 20):
        conflict = db.execute(
            text("SELECT 1 FROM public.subjects WHERE slug = :slug"), {"slug": slug}
        ).fetchone()
        if not conflict:
            break
        slug = f"{base}_{i}"
    result = db.execute(
        text(
            "INSERT INTO public.subjects (id, slug, name, position) "
            "VALUES (:id, :slug, :name, 99) "
            "ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name "
            "RETURNING id"
        ),
        {"id": str(uuid4()), "slug": slug, "name": name},
    ).fetchone()
    return str(result[0])


def _upsert_level(code: str, db: Session) -> str:
    """Get or create a level by code. Returns level UUID as str."""
    result = db.execute(
        text(
            "INSERT INTO public.levels (id, code, label, position) "
            "VALUES (:id, :code, :code, 99) "
            "ON CONFLICT (code) DO UPDATE SET label = public.levels.label "
            "RETURNING id"
        ),
        {"id": str(uuid4()), "code": code},
    ).fetchone()
    return str(result[0])


# Frontend teaching-mode values → DB enum (teaching_mode)
_MODE_MAP = {
    "at_student": "presentiel",
    "at_home": "presentiel",
    "online": "online",
    "hybrid": "hybrid",
    "presentiel": "presentiel",
}


def _insert_user_role(uid: UUID, db: Session) -> None:
    """Ensure (user_id, student) row exists in user_roles."""
    _insert_role(uid, "student", db)


def _insert_role(uid: UUID, role: str, db: Session) -> None:
    """Idempotently insert a role into user_roles."""
    db.execute(
        text(
            "INSERT INTO public.user_roles (user_id, role) "
            "VALUES (:uid, :role) "
            "ON CONFLICT (user_id, role) DO NOTHING"
        ),
        {"uid": str(uid), "role": role},
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


# ─────────────────────────── teacher schemas ─────────────────────────────────

class TeacherSubjectPayload(BaseModel):
    subject: str
    levels: List[str] = []
    price_single: Optional[int] = None
    price_pack5: Optional[int] = None
    price_monthly: Optional[int] = None


class TeacherDiplomaUpload(BaseModel):
    name: str
    url: str
    file_type: Optional[str] = None


class TeacherOnboardingRequest(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None
    wilaya: Optional[str] = None
    bio: Optional[str] = None
    avatar_url: Optional[str] = None
    teaching_modes: Optional[List[str]] = None
    teaching_wilaya: Optional[str] = None
    subjects: Optional[List[TeacherSubjectPayload]] = None
    cover_letter: Optional[str] = None
    # Pre-uploaded file URLs (uploaded via /teacher/upload-document)
    cv_url: Optional[str] = None
    diploma_uploads: Optional[List[TeacherDiplomaUpload]] = None


# ─────────────────────────── teacher upload-document ─────────────────────────

@router.post("/teacher/upload-document")
async def upload_teacher_document(
    file: UploadFile = File(...),
    doc_type: str = Form("diploma"),   # "cv" or "diploma"
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Upload a single document (CV or diploma) to Supabase Storage.
    Returns the public URL to be stored in the teacher onboarding store.
    CV uploads use a fixed path (overwrite on re-upload).
    Diploma uploads use a unique path per file.
    """
    from app.services.storage import upload_file as _upload
    uid = current_user["id"]

    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    file_bytes = await file.read()
    if len(file_bytes) == 0:
        raise HTTPException(status_code=400, detail="Empty file")

    if doc_type == "cv":
        ext = (file.filename.rsplit(".", 1)[-1] if "." in file.filename else "pdf")
        folder = f"documents/{uid}/cv"
        filename = f"cv.{ext}"
    else:
        folder = f"documents/{uid}/diplomas"
        filename = file.filename

    try:
        url = _upload(
            file_bytes=file_bytes,
            filename=filename,
            content_type=file.content_type or "application/octet-stream",
            folder=folder,
        )
    except Exception as exc:
        logger.error("Document upload failed for %s: %s", uid, exc)
        raise HTTPException(status_code=500, detail=f"Upload failed: {exc}")

    return {"url": url, "name": file.filename, "doc_type": doc_type}


# ─────────────────────────── teacher complete ─────────────────────────────────

@router.post("/teacher/complete")
async def complete_teacher_onboarding(
    payload: TeacherOnboardingRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Save ALL teacher onboarding data in one JSON call (from the pending page).

    Files are pre-uploaded via POST /api/onboarding/teacher/upload-document.
    This endpoint receives the resulting URLs (cv_url, diploma_uploads).

    Writes to:
      - public.profiles           (name, phone, bio, avatar_url, wilaya)
      - public.teacher_profiles   (status=pending, teaching_wilaya, cv_url, onboarding_completed=True)
      - public.teacher_modes      (teaching modes)
      - public.teacher_subject_prices (teacher×subject×level with pricing)
      - public.teacher_diplomas   (diploma records with pre-uploaded Storage URLs)
      - public.user_roles         (role = 'teacher')
    """
    data = payload
    uid = UUID(current_user["id"])

    # ── 1. Resolve avatar ─────────────────────────────────────────────────────
    avatar_url = data.avatar_url
    if avatar_url and avatar_url.startswith("data:"):
        uploaded = _upload_base64_avatar(avatar_url, str(uid))
        avatar_url = uploaded

    # ── 2. CV URL (pre-uploaded) ──────────────────────────────────────────────
    cv_url: Optional[str] = data.cv_url

    # ── 3. Diploma records (pre-uploaded) ────────────────────────────────────
    diploma_records: List[Dict] = [
        {"name": d.name, "file_url": d.url, "file_type": d.file_type}
        for d in (data.diploma_uploads or [])
    ]

    # ── 4. Profile ────────────────────────────────────────────────────────────
    profile = db.exec(select(Profile).where(Profile.id == uid)).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")

    if data.first_name:
        profile.first_name = data.first_name
    if data.last_name is not None:
        profile.last_name = data.last_name
    if data.phone:
        profile.phone = data.phone
    if data.bio:
        profile.bio = data.bio
    if avatar_url:
        profile.avatar_url = avatar_url
    if data.wilaya:
        profile.wilaya = data.wilaya
    profile.updated_at = datetime.utcnow()
    db.add(profile)

    # ── 5. TeacherProfile ─────────────────────────────────────────────────────
    tp = db.exec(select(TeacherProfile).where(TeacherProfile.user_id == uid)).first()
    if tp is None:
        tp = TeacherProfile(user_id=uid)

    tp.status = "pending"
    tp.headline = data.bio or ""
    tp.bio_long = data.bio or ""
    if data.teaching_wilaya:
        tp.teaching_wilaya = data.teaching_wilaya
    if cv_url:
        tp.cv_url = cv_url
    db.add(tp)

    # ── 6. Main commit (profile + teacher_profile) ────────────────────────────
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("Teacher onboarding profile commit failed for %s: %s", uid, exc)
        raise HTTPException(status_code=500, detail=f"Onboarding save failed: {exc}")

    # ── 7. Teaching modes (delete + re-insert) ────────────────────────────────
    try:
        db.execute(
            text("DELETE FROM public.teacher_modes WHERE teacher_id = :tid"),
            {"tid": str(uid)},
        )
        seen_modes: set = set()
        for mode in (data.teaching_modes or []):
            db_mode = _MODE_MAP.get(mode, mode)
            if db_mode in seen_modes:
                continue
            seen_modes.add(db_mode)
            db.execute(
                text(
                    "INSERT INTO public.teacher_modes (teacher_id, mode) "
                    "VALUES (:tid, :mode) ON CONFLICT DO NOTHING"
                ),
                {"tid": str(uid), "mode": db_mode},
            )
        db.commit()
    except Exception as exc:
        logger.warning("Teacher modes insert failed for %s: %s", uid, exc)
        db.rollback()

    # ── 8. Offerings: teacher × subject × level × pricing ────────────────────
    # One row per (subject, level) combination in teacher_subject_prices.
    # This replaces the old split teacher_subjects + teacher_levels tables and
    # preserves the exact "teacher teaches Physics to 1AS and 3AS" relationship.
    try:
        db.execute(
            text("DELETE FROM public.teacher_subject_prices WHERE teacher_id = :tid"),
            {"tid": str(uid)},
        )
        for entry in (data.subjects or []):
            subj_id = _upsert_subject(entry.subject, db)
            for level_code in entry.levels:
                level_id = _upsert_level(level_code, db)
                db.execute(
                    text(
                        "INSERT INTO public.teacher_subject_prices "
                        "(teacher_id, subject_id, level_id, price_single, price_pack5, price_monthly) "
                        "VALUES (:tid, :sid, :lid, :ps, :pp, :pm) "
                        "ON CONFLICT (teacher_id, subject_id, level_id) DO UPDATE SET "
                        "price_single = EXCLUDED.price_single, "
                        "price_pack5  = EXCLUDED.price_pack5, "
                        "price_monthly = EXCLUDED.price_monthly, "
                        "updated_at   = now()"
                    ),
                    {
                        "tid": str(uid),
                        "sid": subj_id,
                        "lid": level_id,
                        "ps": entry.price_single or 0,
                        "pp": entry.price_pack5 or 0,
                        "pm": entry.price_monthly or 0,
                    },
                )
        db.commit()
    except Exception as exc:
        logger.warning("Teacher offerings insert failed for %s: %s", uid, exc)
        db.rollback()

    # ── 9. Diploma records ────────────────────────────────────────────────────
    try:
        for d in diploma_records:
            db.execute(
                text(
                    "INSERT INTO public.teacher_diplomas (teacher_id, name, file_url, file_type) "
                    "VALUES (:tid, :name, :url, :ftype)"
                ),
                {"tid": str(uid), "name": d["name"], "url": d["file_url"], "ftype": d["file_type"]},
            )
        db.commit()
    except Exception as exc:
        logger.warning("Diploma records insert failed for %s: %s", uid, exc)
        db.rollback()

    # ── 10. user_roles ────────────────────────────────────────────────────────
    try:
        _insert_role(uid, "teacher", db)
        db.commit()
    except Exception as exc:
        logger.warning("Teacher role insert failed for %s: %s", uid, exc)
        db.rollback()

    # ── 11. Mark onboarding complete + fix JWT role ───────────────────────────
    # onboarding_completed = True so the teacher can access their dashboard
    # while waiting for admin approval (status stays "pending" in teacher_profiles).
    # Also update Supabase app_metadata.role so the next JWT refresh returns "teacher".
    try:
        profile_fresh = db.exec(select(Profile).where(Profile.id == uid)).first()
        if profile_fresh:
            profile_fresh.onboarding_completed = True
            db.add(profile_fresh)
            db.commit()
    except Exception as exc:
        logger.warning("onboarding_completed update failed for teacher %s: %s", uid, exc)
        db.rollback()

    try:
        get_supabase_service().auth.admin.update_user_by_id(
            str(uid),
            {"app_metadata": {"role": "teacher"}},
        )
    except Exception as exc:
        logger.warning("Supabase app_metadata role update failed for %s: %s", uid, exc)

    logger.info(
        "Teacher onboarding submitted: uid=%s modes=%s subjects=%s diplomas=%d",
        uid,
        data.teaching_modes or [],
        [s.subject for s in (data.subjects or [])],
        len(diploma_records),
    )

    return {"status": "pending", "teacher_id": str(uid)}


# ─────────────────────────── parent schemas ───────────────────────────────────

class ParentChildPayload(BaseModel):
    student_code: str
    nickname: Optional[str] = None


class ParentOnboardingRequest(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None
    wilaya: Optional[str] = None
    avatar_url: Optional[str] = None
    children: Optional[List[ParentChildPayload]] = None
    budget_tier: Optional[str] = None
    budget_min: Optional[int] = None
    budget_max: Optional[int] = None


# ─────────────────────────── parent complete ──────────────────────────────────

@router.post("/parent/complete")
async def complete_parent_onboarding(
    payload: ParentOnboardingRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Save ALL parent onboarding data in one call (from the welcome step).

    Writes to:
      - public.profiles              (name, phone, avatar_url, wilaya)
      - public.parent_profiles       (parent_code, budget)
      - public.parent_student_links  (for each matched student code)
      - public.user_roles            (role = 'parent')
      - public.kp_transactions       (+20 EP welcome bonus)

    Sets onboarding_completed = True immediately (parents don't need admin approval).
    """
    uid = UUID(current_user["id"])

    # ── 1. Resolve avatar ─────────────────────────────────────────────────────
    avatar_url = payload.avatar_url
    if avatar_url and avatar_url.startswith("data:"):
        uploaded = _upload_base64_avatar(avatar_url, str(uid))
        avatar_url = uploaded

    # ── 2. Profile ────────────────────────────────────────────────────────────
    profile = db.exec(select(Profile).where(Profile.id == uid)).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")

    if payload.first_name:
        profile.first_name = payload.first_name
    if payload.last_name is not None:
        profile.last_name = payload.last_name
    if payload.phone:
        profile.phone = payload.phone
    if avatar_url:
        profile.avatar_url = avatar_url
    if payload.wilaya:
        profile.wilaya = payload.wilaya
    profile.onboarding_completed = True
    profile.updated_at = datetime.utcnow()
    db.add(profile)

    # ── 3. ParentProfile ──────────────────────────────────────────────────────
    pp = db.exec(select(ParentProfile).where(ParentProfile.user_id == uid)).first()
    if pp is None:
        pp = ParentProfile(
            user_id=uid,
            parent_code=_generate_parent_code(db),
        )

    if payload.budget_tier:
        pp.budget_tier = payload.budget_tier
    if payload.budget_min is not None:
        pp.budget_min = payload.budget_min
    if payload.budget_max is not None:
        pp.budget_max = payload.budget_max
    db.add(pp)

    # ── 4. Commit profile + parent_profile ────────────────────────────────────
    try:
        db.commit()
        db.refresh(pp)
    except Exception as exc:
        db.rollback()
        logger.error("Parent onboarding commit failed for %s: %s", uid, exc)
        raise HTTPException(status_code=500, detail=f"Onboarding save failed: {exc}")

    # ── 5. Link children by student code ──────────────────────────────────────
    children_linked = 0
    try:
        for child in (payload.children or []):
            sp = db.exec(
                select(StudentProfile).where(
                    StudentProfile.student_code == child.student_code.strip().upper()
                )
            ).first()
            if sp:
                existing = db.exec(
                    select(ParentStudentLink)
                    .where(ParentStudentLink.parent_id == uid)
                    .where(ParentStudentLink.student_id == sp.user_id)
                ).first()
                if not existing:
                    db.add(ParentStudentLink(
                        parent_id=uid,
                        student_id=sp.user_id,
                        status="accepted",
                    ))
                children_linked += 1
        db.commit()
    except Exception as exc:
        logger.warning("Children link failed for parent %s: %s", uid, exc)
        db.rollback()

    # ── 6. user_roles + fix JWT role ──────────────────────────────────────────
    try:
        _insert_role(uid, "parent", db)
        db.commit()
    except Exception as exc:
        logger.warning("Parent role insert failed for %s: %s", uid, exc)
        db.rollback()

    try:
        get_supabase_service().auth.admin.update_user_by_id(
            str(uid),
            {"app_metadata": {"role": "parent"}},
        )
    except Exception as exc:
        logger.warning("Supabase app_metadata role update failed for %s: %s", uid, exc)

    # ── 7. Welcome KP bonus (+20 EP) ─────────────────────────────────────────
    _award_welcome_kp(uid, db, amount=20)

    # ── 8. Welcome email (non-fatal) ──────────────────────────────────────────
    try:
        from app.workers.email_tasks import send_welcome_email
        send_welcome_email.delay(profile.email or "", profile.full_name or "")
    except Exception:
        pass

    logger.info(
        "Parent onboarding complete: uid=%s parent_code=%s children=%d",
        uid, pp.parent_code, children_linked,
    )

    return {
        "parent_code": pp.parent_code,
        "children_linked": children_linked,
        "avatar_url": profile.avatar_url,
    }


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
