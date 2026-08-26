from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, select

from app.database import get_supabase_service
from app.dependencies import get_admin_user, get_db
from app.models.catalog import Level, Subject, TeacherDiploma, TeacherSubjectPrice
from app.models.kp import KpTransaction
from app.models.profile import Profile, TeacherProfile
from app.services.tenure import experience_years_from

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Admin — Teachers"])


# ─── Payload schemas ──────────────────────────────────────────────────────────

class RejectPayload(BaseModel):
    reason: str


class SuspendPayload(BaseModel):
    reason: Optional[str] = None


# ─── Internal helpers ─────────────────────────────────────────────────────────

def _subjects_for(teacher_id: UUID, db: Session) -> tuple[list[dict], str]:
    """Return (subjects_list, short_summary_string)."""
    rows = db.exec(
        select(TeacherSubjectPrice, Subject, Level)
        .join(Subject, Subject.id == TeacherSubjectPrice.subject_id)
        .join(Level, Level.id == TeacherSubjectPrice.level_id)
        .where(TeacherSubjectPrice.teacher_id == teacher_id)
        .where(TeacherSubjectPrice.active == True)  # noqa: E712
    ).all()

    subjects: list[dict] = []
    seen_names: list[str] = []
    seen_set: set[str] = set()
    for tsp, subj, lvl in rows:
        subjects.append({"name": subj.name, "level": lvl.label, "price": tsp.price_single})
        if subj.name not in seen_set:
            seen_set.add(subj.name)
            seen_names.append(subj.name)

    summary = ", ".join(seen_names[:3])
    if len(seen_names) > 3:
        summary += f" +{len(seen_names) - 3}"
    return subjects, summary or "—"


def _diplomas_for(teacher_id: UUID, db: Session) -> list[dict]:
    rows = db.exec(
        select(TeacherDiploma).where(TeacherDiploma.teacher_id == teacher_id)
    ).all()
    return [
        {"id": str(d.id), "name": d.name, "file_url": d.file_url, "file_type": d.file_type}
        for d in rows
    ]


def _full_name(p: Profile) -> str:
    name = (p.full_name or "").strip()
    if not name:
        name = f"{p.first_name or ''} {p.last_name or ''}".strip()
    return name or "—"


def _reliability_stats_for(teacher_id: UUID, db: Session) -> dict:
    """
    Acceptance rate is deliberately NOT part of the automatic strike/
    suspension system (see app/services/booking_safety.py — refusing a
    request quickly and honestly is never auto-punished, since it's the
    best-case outcome after a request). It's admin-VISIBILITY only, to spot
    a teacher who refuses so much it looks like a service-quality problem,
    without pressuring teachers into bad acceptances that lead to worse
    downstream no-shows/cancellations.
    """
    from app.models.booking import Booking

    rows = db.exec(
        select(Booking.status, Booking.cancelled_reason).where(Booking.teacher_id == teacher_id)
    ).all()
    accepted = sum(1 for status, _ in rows if status in ("confirmed",))
    refused = sum(1 for status, reason in rows if status == "cancelled" and reason == "teacher_refused")
    no_response = sum(1 for status, reason in rows if status == "cancelled" and reason == "teacher_no_response")
    decided = accepted + refused + no_response
    acceptance_rate = round(accepted / decided * 100) if decided else None

    tp = db.get(TeacherProfile, teacher_id)
    return {
        "acceptance_rate_percent": acceptance_rate,
        "bookings_accepted": accepted,
        "bookings_refused": refused,
        "bookings_no_response": no_response,
        "reliability_strikes": tp.no_response_strikes if tp else 0,
        "reliability_last_incident_at": tp.last_no_response_at.isoformat() if tp and tp.last_no_response_at else None,
        "suspension_reason": tp.suspension_reason if tp else None,
        "suspended_until": tp.suspended_until.isoformat() if tp and tp.suspended_until else None,
    }


def _notify(db: Session, user_id: UUID, title: str, body: str) -> None:
    from app.services.notification_engine import emit
    emit(db, event_type="system", user_id=user_id, title_override=title, body_override=body)


def _fire_approval_email(p: Profile) -> None:
    if not p.email:
        return
    try:
        from app.workers.email_tasks import send_teacher_approved_email
        send_teacher_approved_email.delay(
            to=p.email,
            name=p.first_name or _full_name(p),
        )
    except Exception as exc:
        logger.warning("Approval email skipped: %s", exc)


def _fire_rejection_email(p: Profile, reason: str) -> None:
    if not p.email:
        return
    try:
        from app.workers.email_tasks import send_teacher_rejected_email
        send_teacher_rejected_email.delay(
            to=p.email,
            name=p.first_name or _full_name(p),
            reason=reason,
        )
    except Exception as exc:
        logger.warning("Rejection email skipped: %s", exc)


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/pending")
async def list_pending_teachers(
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=100),
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """List teachers waiting for approval, oldest first (FIFO for SLA)."""
    stmt = (
        select(TeacherProfile, Profile)
        .join(Profile, Profile.id == TeacherProfile.user_id)
        .where(TeacherProfile.status == "pending")
        .order_by(TeacherProfile.created_at.asc())
    )
    all_rows = db.exec(stmt).all()
    total = len(all_rows)
    page_rows = all_rows[(page - 1) * size : page * size]

    items = []
    for tp, p in page_rows:
        subjects, _ = _subjects_for(tp.user_id, db)
        diplomas = _diplomas_for(tp.user_id, db)
        items.append({
            "user_id": str(tp.user_id),
            "full_name": _full_name(p),
            "email": p.email or "—",
            "avatar_url": p.avatar_url,
            "wilaya": p.wilaya,
            "headline": tp.headline,
            "bio_long": tp.bio_long or p.bio,
            "cover_letter": tp.cover_letter,
            "cv_url": tp.cv_url,
            "experience_years": experience_years_from(tp.created_at),
            "price_per_session": tp.price_per_session,
            "currency": tp.currency,
            "teaching_nationwide": tp.teaching_nationwide,
            "subjects": subjects,
            "diplomas": diplomas,
            "submitted_at": tp.created_at.isoformat(),
        })

    return {
        "items": items,
        "total": total,
        "page": page,
        "size": size,
        "pages": math.ceil(total / size) if total else 1,
    }


@router.get("/active")
async def list_active_teachers(
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=100),
    search: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """List approved/suspended teachers with optional search."""
    stmt = (
        select(TeacherProfile, Profile)
        .join(Profile, Profile.id == TeacherProfile.user_id)
        .where(TeacherProfile.status.in_(["approved", "suspended"]))
    )
    if status_filter in ("approved", "suspended"):
        stmt = stmt.where(TeacherProfile.status == status_filter)

    all_rows = db.exec(stmt).all()

    if search:
        q = search.strip().lower()
        all_rows = [
            (tp, p) for tp, p in all_rows
            if q in _full_name(p).lower() or q in (p.email or "").lower()
        ]

    all_rows.sort(key=lambda r: _full_name(r[1]).lower())
    total = len(all_rows)
    page_rows = all_rows[(page - 1) * size : page * size]

    items = []
    for tp, p in page_rows:
        _, summary = _subjects_for(tp.user_id, db)
        items.append({
            "user_id": str(tp.user_id),
            "full_name": _full_name(p),
            "email": p.email or "—",
            "avatar_url": p.avatar_url,
            "wilaya": p.wilaya,
            "headline": tp.headline,
            "rating_avg": float(tp.rating_avg),
            "reviews_count": tp.reviews_count,
            "students_count": tp.students_count,
            "hours_taught": tp.hours_taught,
            "status": tp.status,
            "verified": tp.verified,
            "price_per_session": tp.price_per_session,
            "currency": tp.currency,
            "subjects_summary": summary,
            "created_at": tp.created_at.isoformat(),
            **_reliability_stats_for(tp.user_id, db),
        })

    return {
        "items": items,
        "total": total,
        "page": page,
        "size": size,
        "pages": math.ceil(total / size) if total else 1,
    }


@router.get("/{user_id}")
async def get_teacher_detail(
    user_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Full teacher profile for the admin review modal."""
    tp = db.exec(select(TeacherProfile).where(TeacherProfile.user_id == user_id)).first()
    if tp is None:
        raise HTTPException(status_code=404, detail="Teacher profile not found")

    p = db.exec(select(Profile).where(Profile.id == user_id)).first()
    if p is None:
        raise HTTPException(status_code=404, detail="User profile not found")

    subjects, summary = _subjects_for(user_id, db)
    diplomas = _diplomas_for(user_id, db)

    return {
        "user_id": str(user_id),
        "full_name": _full_name(p),
        "email": p.email or "—",
        "avatar_url": p.avatar_url,
        "wilaya": p.wilaya,
        "headline": tp.headline,
        "bio_long": tp.bio_long or p.bio,
        "cover_letter": tp.cover_letter,
        "cv_url": tp.cv_url,
        "experience_years": experience_years_from(tp.created_at),
        "price_per_session": tp.price_per_session,
        "currency": tp.currency,
        "teaching_nationwide": tp.teaching_nationwide,
        "subjects": subjects,
        "subjects_summary": summary,
        "diplomas": diplomas,
        "rating_avg": float(tp.rating_avg),
        "reviews_count": tp.reviews_count,
        "students_count": tp.students_count,
        "hours_taught": tp.hours_taught,
        "status": tp.status,
        "verified": tp.verified,
        "submitted_at": tp.created_at.isoformat(),
        **_reliability_stats_for(tp.user_id, db),
    }


@router.post("/{user_id}/approve")
async def approve_teacher(
    user_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Approve a pending teacher: status='approved', verified=True, +50 EP, notification, email."""
    tp = db.exec(select(TeacherProfile).where(TeacherProfile.user_id == user_id)).first()
    if tp is None:
        raise HTTPException(status_code=404, detail="Teacher profile not found")
    if tp.status == "approved":
        raise HTTPException(status_code=400, detail="Teacher is already approved")

    p = db.exec(select(Profile).where(Profile.id == user_id)).first()

    tp.status = "approved"
    tp.verified = True
    db.add(tp)

    db.add(KpTransaction(
        user_id=user_id,
        amount=50,
        source="bonus",
        label="Profil enseignant validé — Bienvenue sur E-NOVAR !",
        ref_type="teacher_approval",
    ))

    _notify(
        db, user_id,
        title="🎉 Profil validé !",
        body=(
            "Félicitations ! Votre profil enseignant a été approuvé. "
            "Vous pouvez dès maintenant configurer vos disponibilités et recevoir des réservations. "
            "50 EP vous ont été offerts en cadeau de bienvenue."
        ),
    )

    db.commit()
    if p:
        _fire_approval_email(p)

    logger.info("Teacher approved: user_id=%s by admin=%s", user_id, _admin.get("email", "?"))
    return {"status": "approved", "user_id": str(user_id)}


@router.post("/{user_id}/reject")
async def reject_teacher(
    user_id: UUID,
    payload: RejectPayload,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Reject a pending teacher: send email with reason, then delete Supabase account (cascades)."""
    reason = (payload.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="Un motif de refus est obligatoire.")

    tp = db.exec(select(TeacherProfile).where(TeacherProfile.user_id == user_id)).first()
    if tp is None:
        raise HTTPException(status_code=404, detail="Teacher profile not found")
    if tp.status != "pending":
        raise HTTPException(status_code=400, detail="Seuls les enseignants en attente peuvent être refusés.")

    p = db.exec(select(Profile).where(Profile.id == user_id)).first()

    # Fire email before deletion (profile row still exists with email data)
    if p:
        _fire_rejection_email(p, reason)

    # Delete from Supabase auth — cascades: auth.users → profiles → teacher_profiles → teacher_diplomas
    try:
        get_supabase_service().auth.admin.delete_user(str(user_id))
    except Exception as exc:
        logger.error(
            "Failed to delete rejected teacher from Supabase: user_id=%s err=%s", user_id, exc
        )
        raise HTTPException(status_code=500, detail="Erreur lors de la suppression du compte.")

    logger.info(
        "Teacher rejected + deleted: user_id=%s reason=%r by admin=%s",
        user_id, reason, _admin.get("email", "?"),
    )
    return {"status": "rejected", "user_id": str(user_id)}


@router.post("/{user_id}/suspend")
async def suspend_teacher(
    user_id: UUID,
    payload: SuspendPayload,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Suspend an active teacher."""
    tp = db.exec(select(TeacherProfile).where(TeacherProfile.user_id == user_id)).first()
    if tp is None:
        raise HTTPException(status_code=404, detail="Teacher profile not found")
    if tp.status == "suspended":
        raise HTTPException(status_code=400, detail="Teacher is already suspended")
    if tp.status == "pending":
        raise HTTPException(status_code=400, detail="Cannot suspend a pending teacher. Reject instead.")

    reason_text = (payload.reason or "").strip() or "Votre compte a été suspendu temporairement."
    tp.status = "suspended"
    db.add(tp)
    _notify(
        db, user_id,
        title="⚠️ Compte suspendu",
        body=f"Votre compte enseignant a été suspendu. Motif : {reason_text}",
    )
    db.commit()

    logger.info("Teacher suspended: user_id=%s reason=%r", user_id, payload.reason)
    return {"status": "suspended", "user_id": str(user_id)}


@router.post("/{user_id}/reinstate")
async def reinstate_teacher(
    user_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Reinstate a suspended teacher."""
    tp = db.exec(select(TeacherProfile).where(TeacherProfile.user_id == user_id)).first()
    if tp is None:
        raise HTTPException(status_code=404, detail="Teacher profile not found")
    if tp.status != "suspended":
        raise HTTPException(status_code=400, detail="Teacher is not suspended")

    tp.status = "approved"
    db.add(tp)
    _notify(
        db, user_id,
        title="✅ Compte réactivé",
        body="Votre compte enseignant a été réactivé. Vous pouvez à nouveau recevoir des réservations.",
    )
    db.commit()

    logger.info("Teacher reinstated: user_id=%s", user_id)
    return {"status": "approved", "user_id": str(user_id)}
