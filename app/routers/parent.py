from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db, require_role
from app.models.booking import Booking, TutoringSession
from app.models.catalog import Subject
from app.models.kp import KpBalance, KpTransaction
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
    now_utc = datetime.now(timezone.utc)

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


class LinkChildRequest(BaseModel):
    student_code: str


@router.post("/children/link")
async def link_child(
    payload: LinkChildRequest,
    current_user: Dict[str, Any] = Depends(require_role("parent")),
    db: Session = Depends(get_db),
):
    """Link an additional child post-onboarding — same lookup/creation logic
    as the parent-onboarding batch link in app/routers/onboarding.py (by
    StudentProfile.student_code), just usable one child at a time afterwards."""
    uid = UUID(current_user["id"])
    code = payload.student_code.strip().upper()

    sp = db.exec(select(StudentProfile).where(StudentProfile.student_code == code)).first()
    if sp is None:
        raise HTTPException(status_code=404, detail="Aucun élève trouvé avec ce code.")

    existing = db.exec(
        select(ParentStudentLink)
        .where(ParentStudentLink.parent_id == uid)
        .where(ParentStudentLink.student_id == sp.user_id)
    ).first()
    if existing and existing.status == "accepted":
        raise HTTPException(status_code=409, detail="Cet enfant est déjà lié à votre compte.")

    now = datetime.now(timezone.utc)
    if existing:
        existing.status = "accepted"
        existing.accepted_at = now
        db.add(existing)
    else:
        db.add(ParentStudentLink(parent_id=uid, student_id=sp.user_id, status="accepted", accepted_at=now))

    student_profile = db.exec(select(Profile).where(Profile.id == sp.user_id)).first()
    parent_profile = db.exec(select(Profile).where(Profile.id == uid)).first()
    db.commit()

    from app.services.notification_engine import emit
    emit(
        db, event_type="system", user_id=sp.user_id,
        title_override="👨‍👩‍👧 Compte parent lié",
        body_override=f"{(parent_profile.full_name if parent_profile else None) or 'Un parent'} suit désormais votre compte E-NOVAR.",
        data={"parent_id": str(uid)},
    )

    return {
        "student_id": str(sp.user_id),
        "student_code": sp.student_code,
        "name": (student_profile.full_name if student_profile else None) or "Enfant",
        "avatar_url": student_profile.avatar_url if student_profile else None,
    }


@router.post("/children/{student_id}/unlink")
async def unlink_child(
    student_id: UUID,
    current_user: Dict[str, Any] = Depends(require_role("parent")),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])
    link = db.exec(
        select(ParentStudentLink)
        .where(ParentStudentLink.parent_id == uid)
        .where(ParentStudentLink.student_id == student_id)
        .where(ParentStudentLink.status == "accepted")
    ).first()
    if link is None:
        raise HTTPException(status_code=404, detail="Liaison introuvable")

    link.status = "revoked"
    db.add(link)
    db.commit()

    from app.services.notification_engine import emit
    emit(
        db, event_type="system", user_id=student_id,
        title_override="🔗 Liaison retirée",
        body_override="Votre parent s'est dissocié de votre compte. Vous gérez maintenant votre espace en autonomie.",
        data={"parent_id": str(uid)},
    )
    return {"status": "revoked"}


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

    now_utc = datetime.now(timezone.utc)
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

    now_utc = datetime.now(timezone.utc)
    # A parent cancelling on their child's behalf is always the "student
    # side" — the courtesy time-based tiers apply (teacher-fault cancellation
    # goes through sessions.py's own cancel_session, called by the teacher).
    refund_pct = _compute_refund_pct(session.scheduled_at, "student")

    from app.services.pricing import PACK_SIZES

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

    refund_result = {"refunded": False, "requires_manual_action": False}
    if booking is not None and refund_amount > 0:
        from app.services.refunds import refund_amount_for_booking
        refund_result = refund_amount_for_booking(
            db, booking, refund_amount,
            note=f"Séance annulée par le parent ({refund_pct}% remboursé).",
        )

    return {
        "id": str(session.id),
        "status": "cancelled",
        "refund_percentage": refund_pct,
        "refund_amount": refund_amount,
        "teacher_payout_amount": teacher_payout,
        "refunded": refund_result["refunded"],
        "refund_requires_manual_action": refund_result["requires_manual_action"],
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


# ── Child no-show / booking-suspension history ───────────────────────────────
# Every incident already triggers a notification to the parent directly (see
# app/services/booking_safety.py's apply_student_strike /
# app/services/refunds.py's _notify_parent_of_refund) — this endpoint backs a
# proper detail view instead of only a passing notification.

@router.get("/children/{child_id}/incidents")
async def get_child_incidents(
    child_id: UUID,
    current_user: Dict[str, Any] = Depends(require_role("parent")),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])
    _verify_child_link(uid, child_id, db)

    child_sp = db.exec(select(StudentProfile).where(StudentProfile.user_id == child_id)).first()

    no_show_sessions = db.exec(
        select(TutoringSession)
        .where(TutoringSession.student_id == child_id)
        .where(TutoringSession.no_show == True)  # noqa: E712
        .order_by(TutoringSession.scheduled_at.desc())
    ).all()

    teacher_ids = list({s.teacher_id for s in no_show_sessions})
    teachers = db.exec(select(Profile).where(Profile.id.in_(teacher_ids))).all() if teacher_ids else []
    teacher_map = {t.id: t for t in teachers}

    incidents = []
    for s in no_show_sessions:
        teacher = teacher_map.get(s.teacher_id)
        incidents.append({
            "session_id": str(s.id),
            "scheduled_at": s.scheduled_at.isoformat(),
            "mode": s.mode,
            "teacher_name": teacher.full_name if teacher else "—",
            # cancellation_reason distinguishes fault: "student_no_show" (the
            # child's own absence) vs "teacher_no_show" (not the child's
            # fault — refunded, no impact on the child's own record).
            "reason": s.cancellation_reason,
            "at_fault": "student" if s.cancellation_reason == "student_no_show" else "teacher",
            "refund_amount": s.refund_amount,
        })

    return {
        "student_id": str(child_id),
        "no_show_strikes": child_sp.no_show_strikes if child_sp else 0,
        "last_no_show_at": child_sp.last_no_show_at.isoformat() if child_sp and child_sp.last_no_show_at else None,
        "booking_suspended_until": child_sp.booking_suspended_until.isoformat() if child_sp and child_sp.booking_suspended_until else None,
        "booking_suspension_reason": child_sp.booking_suspension_reason if child_sp else None,
        "incidents": incidents,
    }


# ── Child KP balance ─────────────────────────────────────────────────────────

@router.get("/children/{child_id}/kp/balance")
async def get_child_kp_balance(
    child_id: UUID,
    current_user: Dict[str, Any] = Depends(require_role("parent")),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])
    _verify_child_link(uid, child_id, db)

    kp = db.exec(select(KpBalance).where(KpBalance.user_id == child_id)).first()
    if kp is None:
        return {
            "user_id": str(child_id),
            "balance": 0,
            "total_earned": 0,
            "week_earned": 0,
            "level": 1,
            "xp": 0,
            "next_level_at": 500,
            "streak_days": 0,
        }
    return {
        "user_id": str(kp.user_id),
        "balance": kp.balance,
        "total_earned": kp.total_earned,
        "week_earned": kp.week_earned,
        "level": kp.level,
        "xp": kp.xp,
        "next_level_at": kp.next_level_at,
        "streak_days": 0,
    }


# ── Child KP transactions ─────────────────────────────────────────────────────

@router.get("/children/{child_id}/kp/transactions")
async def get_child_kp_transactions(
    child_id: UUID,
    page: int = Query(1, ge=1),
    size: int = Query(10, ge=1, le=50),
    current_user: Dict[str, Any] = Depends(require_role("parent")),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])
    _verify_child_link(uid, child_id, db)

    all_txs = db.exec(
        select(KpTransaction)
        .where(KpTransaction.user_id == child_id)
        .order_by(KpTransaction.created_at.desc())
    ).all()

    total = len(all_txs)
    offset = (page - 1) * size
    page_txs = all_txs[offset: offset + size]

    return {
        "items": [
            {
                "id": str(t.id),
                "user_id": str(t.user_id),
                "label": t.label or "",
                "source": t.source,
                "amount": t.amount,
                "balance_after": None,
                "created_at": t.created_at.isoformat(),
            }
            for t in page_txs
        ],
        "total": total,
        "page": page,
        "size": size,
        "pages": math.ceil(total / size) if total else 0,
    }


# ── Redeem a store item on behalf of a linked child ──────────────────────────
# The child's own KP account is debited and credited with whatever the store
# item grants — same underlying app.services.store.redeem_item used by
# POST /api/store/items/{item_id}/redeem, just targeting child_id instead of
# the caller's own id (verified as an accepted link first).

@router.post("/children/{child_id}/store/redeem", status_code=status.HTTP_201_CREATED)
async def redeem_store_item_for_child(
    child_id: UUID,
    item_id: str = Body(..., embed=True),
    current_user: Dict[str, Any] = Depends(require_role("parent")),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])
    _verify_child_link(uid, child_id, db)

    from app.services.store import redeem_item

    try:
        claim, entitlement, account, msg = redeem_item(child_id, item_id, db)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "message": msg,
        "claim": {
            "id": str(claim.id),
            "item_id": claim.item_id,
            "cost": claim.cost,
            "status": claim.status,
            "claimed_at": claim.claimed_at.isoformat(),
        },
        "new_balance": account.balance,
    }


# ── Parent payments ────────────────────────────────────────────────────────────
# A parent never has bookings of their own — the sessions/bookings they pay
# for always belong to one of their linked children (Booking.student_id).
# This lists every such booking as one "operation", newest first.

@router.get("/payments")
async def get_parent_payments(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    month: Optional[str] = Query(None, description="Filter to a single month, format YYYY-MM"),
    current_user: Dict[str, Any] = Depends(require_role("parent")),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])
    child_ids = [
        link.student_id
        for link in db.exec(
            select(ParentStudentLink)
            .where(ParentStudentLink.parent_id == uid)
            .where(ParentStudentLink.status == "accepted")
        ).all()
    ]
    if not child_ids:
        return {"items": [], "total": 0, "page": page, "size": size, "pages": 0}

    bookings = db.exec(
        select(Booking)
        .where(Booking.student_id.in_(child_ids))
        .order_by(Booking.created_at.desc())
    ).all()

    if month:
        bookings = [b for b in bookings if b.created_at.strftime("%Y-%m") == month]

    total = len(bookings)
    offset = (page - 1) * size
    page_bookings = bookings[offset: offset + size]

    child_map = {p.id: p for p in db.exec(select(Profile).where(Profile.id.in_(child_ids))).all()}
    teacher_ids = list({b.teacher_id for b in page_bookings})
    teacher_map = {p.id: p for p in db.exec(select(Profile).where(Profile.id.in_(teacher_ids))).all()} if teacher_ids else {}

    return {
        "items": [
            {
                "id": str(b.id),
                "child_id": str(b.student_id),
                "child_name": (child_map.get(b.student_id).full_name if child_map.get(b.student_id) else None) or "Enfant",
                "teacher_id": str(b.teacher_id),
                "teacher_name": (teacher_map.get(b.teacher_id).full_name if teacher_map.get(b.teacher_id) else None) or "Enseignant",
                "formula": b.formula,
                "mode": b.mode,
                "amount": b.amount,
                "payment_method": b.payment_method,
                "status": b.status,
                "date": b.booking_date.isoformat() if b.booking_date else None,
                "subject": b.subject,
                "created_at": b.created_at.isoformat(),
            }
            for b in page_bookings
        ],
        "total": total,
        "page": page,
        "size": size,
        "pages": math.ceil(total / size) if total else 0,
    }
