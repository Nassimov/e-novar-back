from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db, require_role
from app.models.enums import StudentLessonFormat
from app.models.parent_link import ParentStudentLink
from app.models.profile import Profile, StudentProfile, TeacherProfile
from app.services.storage import upload_file

router = APIRouter(tags=["profile"])


# ── Response schema compatible with frontend AuthUser ─────────────────────────

class ProfileResponse(BaseModel):
    id: str
    email: str
    full_name: str
    role: str
    avatar_url: Optional[str] = None
    wilaya: Optional[str] = None
    phone: Optional[str] = None
    bio: Optional[str] = None
    onboarding_completed: bool = False
    is_verified: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ProfileUpdateRequest(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    wilaya: Optional[str] = None
    phone: Optional[str] = None
    bio: Optional[str] = None
    avatar_url: Optional[str] = None


class PaymentMethodCreate(BaseModel):
    type: str
    holder_name: str
    card_last4: Optional[str] = None
    iban: Optional[str] = None
    bank_name: Optional[str] = None


class PaymentMethodResponse(BaseModel):
    id: str
    type: str
    holder_name: str
    card_last4: Optional[str] = None
    bank_name: Optional[str] = None
    is_default: bool = False


# ── Helper ────────────────────────────────────────────────────────────────────

def _get_profile(user_id_str: str, db: Session) -> Profile:
    uid = UUID(user_id_str)
    profile = db.exec(select(Profile).where(Profile.id == uid)).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile


def _to_response(profile: Profile, role: str) -> ProfileResponse:
    return ProfileResponse(
        id=str(profile.id),
        email=profile.email or "",
        full_name=profile.full_name or "",
        role=role,
        avatar_url=profile.avatar_url,
        wilaya=profile.wilaya,
        phone=profile.phone,
        bio=profile.bio,
        onboarding_completed=profile.onboarding_completed,
        is_verified=False,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

class StudentFullProfileResponse(BaseModel):
    id: str
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    full_name: Optional[str] = None
    phone: Optional[str] = None
    birth_date: Optional[str] = None
    gender: Optional[str] = None
    avatar_url: Optional[str] = None
    active_sticker_url: Optional[str] = None
    wilaya: Optional[str] = None
    student_code: Optional[str] = None
    parent_link_code: Optional[str] = None
    monitoring_mode: Optional[str] = None
    level_main: Optional[str] = None
    level_detail: Optional[str] = None
    speciality: Optional[str] = None
    subjects_interested: Optional[List[str]] = None
    goals: Optional[List[str]] = None
    online_only: bool = False
    budget_tier: Optional[str] = None
    budget_min: Optional[int] = None
    budget_max: Optional[int] = None
    available_days: Optional[List[int]] = None
    available_slots: Optional[List[str]] = None
    lesson_format: Optional[str] = None
    languages: Optional[List[str]] = None
    # True when an active parent link exists — student cannot edit availability
    parent_defined_availability: bool = False


class StudentProfileUpdateRequest(BaseModel):
    """Partial update — only fields present are touched. Used by the profile
    page (Disponibilités / Mes créneaux disponibles, format de leçon, wilaya,
    langues parlées) as well as any future 'edit later' flow."""
    wilaya: Optional[str] = None
    online_only: Optional[bool] = None
    available_days: Optional[List[int]] = None
    available_slots: Optional[List[str]] = None
    lesson_format: Optional[str] = None
    languages: Optional[List[str]] = None


@router.get("/student", response_model=StudentFullProfileResponse, tags=["Profile"])
async def get_student_profile(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the full profile + student-specific fields for the authenticated student.

    When the student has at least one accepted parent link, the effective
    availability comes from parent_child_availabilities (parent-defined, read-only).
    The student's own student_profiles.available_* columns are never touched.
    """
    uid = UUID(current_user["id"])
    profile = db.exec(select(Profile).where(Profile.id == uid)).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    sp = db.exec(select(StudentProfile).where(StudentProfile.user_id == uid)).first()
    birth = str(profile.birth_date) if profile.birth_date else None

    # ── Determine effective availability ─────────────────────────────────────
    # Check for any accepted parent link for this student.
    parent_link = db.exec(
        select(ParentStudentLink)
        .where(ParentStudentLink.student_id == uid)
        .where(ParentStudentLink.status == "accepted")
    ).first()

    parent_defined_availability = False
    effective_days: Optional[List[int]] = sp.available_days if sp else None
    effective_slots: Optional[List[str]] = sp.available_slots if sp else None

    if parent_link:
        # Fetch all parents' availability arrays for this student and union them.
        # Table schema (migration 002): one row per (parent_id, student_id),
        # columns available_days int[] and available_slots text[].
        rows = db.execute(
            text(
                "SELECT available_slots FROM public.parent_child_availabilities "
                "WHERE student_id = :sid"
            ),
            {"sid": str(uid)},
        ).fetchall()
        all_slots: set[str] = set()
        for row in rows:
            if row[0]:  # row[0] is the available_slots text[] or None
                all_slots.update(row[0])
        parent_defined_availability = True  # lock editing whenever a parent link exists
        if all_slots:
            effective_slots = sorted(all_slots)
            effective_days = sorted(
                {int(s.split(":")[0]) for s in all_slots if ":" in s and s.split(":")[0].isdigit()}
            )

    return StudentFullProfileResponse(
        id=str(profile.id),
        email=profile.email or "",
        first_name=profile.first_name,
        last_name=profile.last_name,
        full_name=profile.full_name,
        phone=profile.phone,
        birth_date=birth,
        gender=profile.gender,
        avatar_url=profile.avatar_url,
        active_sticker_url=profile.active_sticker_url,
        wilaya=profile.wilaya,
        student_code=sp.student_code if sp else None,
        parent_link_code=sp.parent_link_code if sp else None,
        monitoring_mode=sp.monitoring_mode if sp else None,
        level_main=sp.level_main if sp else None,
        level_detail=sp.level_detail if sp else None,
        speciality=sp.speciality if sp else None,
        subjects_interested=sp.subjects_interested if sp else None,
        goals=sp.goals if sp else None,
        online_only=sp.online_only if sp else False,
        budget_tier=sp.budget_tier if sp else None,
        budget_min=sp.budget_min if sp else None,
        budget_max=sp.budget_max if sp else None,
        available_days=effective_days,
        available_slots=effective_slots,
        lesson_format=sp.lesson_format if sp else None,
        languages=sp.languages if sp else None,
        parent_defined_availability=parent_defined_availability,
    )


@router.put("/student", response_model=StudentFullProfileResponse, tags=["Profile"])
async def update_student_profile(
    payload: StudentProfileUpdateRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Partial update of student-specific profile fields.

    Backs the profile page's Disponibilités / Mes créneaux disponibles,
    format de leçon, wilaya/en ligne seulement, and langues parlées sections —
    the same fields collected at onboarding, now editable afterwards.

    Availability (available_days/available_slots) is silently ignored when an
    accepted parent link exists — the parent owns that data in this case, same
    lock already enforced on the read side by GET /student.
    """
    uid = UUID(current_user["id"])
    profile = db.exec(select(Profile).where(Profile.id == uid)).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")

    sp = db.exec(select(StudentProfile).where(StudentProfile.user_id == uid)).first()
    if sp is None:
        sp = StudentProfile(user_id=uid)

    if payload.wilaya is not None:
        profile.wilaya = payload.wilaya
        profile.updated_at = datetime.utcnow()
        db.add(profile)

    if payload.online_only is not None:
        sp.online_only = payload.online_only

    parent_link = db.exec(
        select(ParentStudentLink)
        .where(ParentStudentLink.student_id == uid)
        .where(ParentStudentLink.status == "accepted")
    ).first()
    if parent_link is None:
        if payload.available_days is not None:
            sp.available_days = payload.available_days
        if payload.available_slots is not None:
            sp.available_slots = payload.available_slots

    if payload.lesson_format is not None:
        valid_formats = {f.value for f in StudentLessonFormat}
        if payload.lesson_format not in valid_formats:
            raise HTTPException(
                status_code=422,
                detail=f"lesson_format invalide. Valeurs acceptées : {sorted(valid_formats)}",
            )
        sp.lesson_format = payload.lesson_format

    if payload.languages is not None:
        sp.languages = payload.languages

    db.add(sp)
    db.commit()

    return await get_student_profile(current_user=current_user, db=db)


@router.get("/", response_model=ProfileResponse)
async def get_profile(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get the current user's profile."""
    profile = _get_profile(current_user["id"], db)
    return _to_response(profile, current_user["role"])


@router.put("/", response_model=ProfileResponse)
async def update_profile(
    payload: ProfileUpdateRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update the current user's profile."""
    profile = _get_profile(current_user["id"], db)

    if payload.first_name is not None:
        profile.first_name = payload.first_name
    if payload.last_name is not None:
        profile.last_name = payload.last_name
    if payload.wilaya is not None:
        profile.wilaya = payload.wilaya
    if payload.phone is not None:
        profile.phone = payload.phone
    if payload.bio is not None:
        profile.bio = payload.bio
    if payload.avatar_url is not None:
        profile.avatar_url = payload.avatar_url

    profile.updated_at = datetime.utcnow()
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return _to_response(profile, current_user["role"])


@router.post("/avatar", response_model=ProfileResponse)
async def upload_avatar(
    file: UploadFile,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upload and update user avatar."""
    if file.content_type not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        raise HTTPException(status_code=400, detail="Only image files are allowed")

    contents = await file.read()
    if len(contents) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (max 5MB)")

    url = upload_file(
        contents,
        file.filename or "avatar.jpg",
        file.content_type,
        folder=f"avatars/{current_user['id']}",
    )

    profile = _get_profile(current_user["id"], db)
    profile.avatar_url = url
    profile.updated_at = datetime.utcnow()
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return _to_response(profile, current_user["role"])


@router.get("/teacher", tags=["Profile"])
async def get_teacher_profile(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return teacher-specific profile fields (status, cover_letter, etc.)."""
    uid = UUID(current_user["id"])
    tp = db.exec(select(TeacherProfile).where(TeacherProfile.user_id == uid)).first()
    if tp is None:
        raise HTTPException(status_code=404, detail="Teacher profile not found")
    return {
        "status": tp.status,
        "teaching_wilaya": tp.teaching_wilaya,
        "cv_url": tp.cv_url,
        "cover_letter": tp.cover_letter,
        "headline": tp.headline,
        "experience_years": tp.experience_years,
        "rating_avg": tp.rating_avg,
        "reviews_count": tp.reviews_count,
    }


@router.post("/teacher/{teacher_user_id}/approve", tags=["Admin — Teachers"])
async def approve_teacher(
    teacher_user_id: UUID,
    current_user: Dict[str, Any] = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Approve a teacher account: set status='approved' and award 50 EP."""
    from datetime import datetime
    from app.models.kp import KpTransaction

    tp = db.exec(select(TeacherProfile).where(TeacherProfile.user_id == teacher_user_id)).first()
    if tp is None:
        raise HTTPException(status_code=404, detail="Teacher profile not found")

    tp.status = "approved"
    tp.verified = True
    db.add(tp)

    # Award 50 EP
    try:
        kp = KpTransaction(
            user_id=teacher_user_id,
            amount=50,
            source="bonus",
            label="Profil enseignant validé — Bienvenue sur E-NOVAR !",
            ref_type="admin_approval",
        )
        db.add(kp)
    except Exception:
        pass

    db.commit()
    return {"status": "approved", "teacher_user_id": str(teacher_user_id)}


@router.post("/teacher/{teacher_user_id}/reject", tags=["Admin — Teachers"])
async def reject_teacher(
    teacher_user_id: UUID,
    current_user: Dict[str, Any] = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Reject a teacher account: delete from Supabase auth (cascades to all tables)."""
    tp = db.exec(select(TeacherProfile).where(TeacherProfile.user_id == teacher_user_id)).first()
    if tp is None:
        raise HTTPException(status_code=404, detail="Teacher profile not found")

    try:
        from app.database import get_supabase_service
        get_supabase_service().auth.admin.delete_user(str(teacher_user_id))
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Erreur lors de la suppression du compte.")
    return {"status": "rejected", "teacher_user_id": str(teacher_user_id)}


@router.put("/notifications")
async def update_notification_prefs(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update notification preferences (stub — prefs stored in notification_preferences table)."""
    return {"message": "Notification preferences updated"}


@router.get("/payment-methods", response_model=List[PaymentMethodResponse])
async def list_payment_methods(
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """List saved payment methods (stub — full implementation in payments router)."""
    return []


@router.post(
    "/payment-methods",
    response_model=PaymentMethodResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_payment_method(
    payload: PaymentMethodCreate,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Add a payment method (stub)."""
    import uuid as _uuid
    return PaymentMethodResponse(
        id=str(_uuid.uuid4()),
        type=payload.type,
        holder_name=payload.holder_name,
        card_last4=payload.card_last4,
        bank_name=payload.bank_name,
        is_default=False,
    )


@router.delete("/payment-methods/{method_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_payment_method(
    method_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Remove a saved payment method (stub)."""
    return None
