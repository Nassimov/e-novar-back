from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, select

from app.dependencies import get_admin_user, get_db
from app.models.booking import Booking, TutoringSession
from app.models.notification import Notification
from app.models.profile import Profile, UserRole

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Admin — Bookings"])

# Payment methods an admin can manually approve/reject here.
#
# cash / transfer: no real-time gateway integration exists for these at all
# (no publicly integrable API for BaridiMob/bank transfer reconciliation) —
# this admin review IS their only confirmation path. See
# app/routers/teachers.py _MANUAL_PAYMENT_METHODS (cash/transfer only —
# accept_booking/refuse_booking reject these outright).
#
# edahabia: automated via Chargily Pay (app/services/chargily.py +
# app/routers/chargily_webhook.py) — the normal path is the webhook setting
# `chargily_paid_at`, which lets the teacher accept/refuse directly. It's
# kept in this admin list only as a manual fallback (e.g. a missed webhook
# delivery), not the primary path — never claim "instant"/"automatic" for
# cash/transfer, but edahabia genuinely is once paid.
#
# rib_cib / rib_edahabia: the student's own saved RIB is only used for
# reconciliation — no gateway integration yet (deferred phase, see
# docs/migrations/068), so exactly like cash/transfer this admin review is
# their only confirmation path for now.
MANUAL_PAYMENT_METHODS = ("cash", "edahabia", "transfer", "rib_cib", "rib_edahabia")

_METHOD_LABELS = {
    "cash": "en espèces",
    "edahabia": "Edahabia",
    "transfer": "par virement bancaire",
    "rib_cib": "par virement RIB CIB",
    "rib_edahabia": "par virement RIB Edahabia",
}


class BookingListItem(BaseModel):
    id: str
    student_name: str
    teacher_name: str
    formula: str
    mode: str
    date: Optional[str]
    slot_time: Optional[str]
    amount: int
    payment_method: Optional[str]
    payment_rib: Optional[str] = None
    subject: Optional[str]
    comment: Optional[str]
    status: str
    created_at: str


@router.get("/", response_model=List[BookingListItem])
async def list_bookings(
    payment_method: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """List bookings, optionally filtered by payment_method or status."""
    query = select(Booking)
    if payment_method:
        query = query.where(Booking.payment_method == payment_method)
    if status:
        query = query.where(Booking.status == status)
    query = query.order_by(Booking.created_at.desc())
    bookings = db.exec(query).all()

    profile_ids = list({b.student_id for b in bookings} | {b.teacher_id for b in bookings})
    profiles = db.exec(select(Profile).where(Profile.id.in_(profile_ids))).all()
    prof_map = {p.id: p for p in profiles}

    result: List[BookingListItem] = []
    for b in bookings:
        student = prof_map.get(b.student_id)
        teacher = prof_map.get(b.teacher_id)
        result.append(BookingListItem(
            id=str(b.id),
            student_name=student.full_name or "—" if student else "—",
            teacher_name=teacher.full_name or "—" if teacher else "—",
            formula=b.formula,
            mode=b.mode,
            date=b.booking_date.isoformat() if b.booking_date else None,
            slot_time=str(b.slot_time)[:5] if b.slot_time else None,
            amount=b.amount,
            payment_method=b.payment_method,
            payment_rib=b.payment_rib,
            subject=b.subject,
            comment=b.comment,
            status=b.status,
            created_at=b.created_at.isoformat(),
        ))
    return result


@router.post("/{booking_id}/approve-manual-payment")
async def approve_manual_payment(
    booking_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """
    Admin confirms a manually-reconciled payment (cash / bank transfer — no
    real-time gateway integration exists for these, see
    MANUAL_PAYMENT_METHODS above):
    - confirms the booking directly (status → confirmed) — for cash/transfer,
      admin confirmation IS the acceptance step (there is no separate
      teacher accept/refuse for them, see app/routers/teachers.py
      _MANUAL_PAYMENT_METHODS guard).
    - notifies student and teacher via in-app notification.

    edahabia is normally confirmed automatically by the Chargily webhook
    (see app/routers/chargily_webhook.py) and still goes through the
    teacher's own accept/refuse afterward — this endpoint also accepts
    edahabia, but only as a manual override/fallback (e.g. a missed webhook
    delivery); using it here skips the teacher's decision entirely, so treat
    it as an escape hatch, not the normal path.

    Kept as `/approve-cash-payment` was previously (cash-only) — widened to
    cover all 3 non-Stripe rails since none of them had any confirmation path
    at all before this. No existing frontend caller of the old path (grepped
    — zero hits), so renaming here is safe.
    """
    booking = db.get(Booking, booking_id)
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.payment_method not in MANUAL_PAYMENT_METHODS:
        raise HTTPException(
            status_code=400,
            detail=f"This booking's payment method ('{booking.payment_method}') is not manually confirmed — "
                   f"expected one of {MANUAL_PAYMENT_METHODS}.",
        )
    if booking.status not in ("pending",):
        raise HTTPException(status_code=409, detail=f"Cannot approve booking with status '{booking.status}'")

    booking.status = "confirmed"
    db.add(booking)

    student = db.get(Profile, booking.student_id)
    teacher = db.get(Profile, booking.teacher_id)
    method_label = _METHOD_LABELS.get(booking.payment_method, booking.payment_method)

    def _notify(user_id: UUID, title: str, body: str):
        db.add(Notification(
            user_id=user_id,
            title=title,
            body=body,
            type="booking",
            data={"booking_id": str(booking.id)},
        ))

    if student:
        _notify(
            booking.student_id,
            "✅ Paiement confirmé",
            f"Votre paiement {method_label} pour la réservation du {booking.booking_date} a été validé par l'administration. Votre séance est confirmée.",
        )
    if teacher:
        _notify(
            booking.teacher_id,
            "📬 Réservation confirmée",
            f"Un élève a réservé une séance pour le {booking.booking_date} (paiement {method_label} validé par l'administration).",
        )

    db.commit()
    logger.info("Manual payment approved: booking_id=%s method=%s", booking_id, booking.payment_method)
    return {"status": "confirmed", "booking_id": str(booking_id)}


@router.post("/{booking_id}/reject-manual-payment")
async def reject_manual_payment(
    booking_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Admin rejects (cancels) a booking whose cash/Edahabia/transfer/RIB
    payment could not be verified. For cash/transfer/rib_cib/rib_edahabia,
    rejection itself means "reconciliation failed", i.e. nothing was
    received — nothing to refund. Edahabia is the one exception: Chargily
    may have already charged the student (chargily_paid_at set) even though
    the booking is still 'pending' awaiting the teacher's decision — that
    money is real and needs a human refund, same as refuse_booking already
    handles for this exact case."""
    booking = db.get(Booking, booking_id)
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.payment_method not in MANUAL_PAYMENT_METHODS:
        raise HTTPException(
            status_code=400,
            detail=f"This booking's payment method ('{booking.payment_method}') is not manually confirmed — "
                   f"expected one of {MANUAL_PAYMENT_METHODS}.",
        )
    if booking.status not in ("pending",):
        raise HTTPException(status_code=409, detail=f"Cannot reject booking with status '{booking.status}'")

    booking.status = "cancelled"
    db.add(booking)

    if booking.payment_method == "edahabia" and booking.chargily_paid_at is not None:
        # Chargily has no refund API — already paid, needs a human, same as
        # app/routers/teachers.py's refuse_booking.
        for ar in db.exec(select(UserRole).where(UserRole.role == "admin")).all():
            db.add(Notification(
                user_id=ar.user_id,
                type="system",
                title="⚠️ Remboursement Edahabia manuel requis",
                body=(
                    f"Réservation rejetée par l'administration mais déjà payée en Edahabia "
                    f"({booking.amount} DA) — remboursement manuel requis."
                ),
                data={"booking_id": str(booking.id)},
            ))

    method_label = _METHOD_LABELS.get(booking.payment_method, booking.payment_method)
    student = db.get(Profile, booking.student_id)
    if student:
        db.add(Notification(
            user_id=booking.student_id,
            title="❌ Paiement non validé",
            body=f"Votre paiement {method_label} pour la réservation du {booking.booking_date} n'a pas pu être validé. Contactez le support.",
            type="booking",
            data={"booking_id": str(booking.id)},
        ))

    db.commit()
    return {"status": "cancelled", "booking_id": str(booking_id)}


@router.post("/{booking_id}/simulate-completion")
async def simulate_completion(
    booking_id: UUID,
    admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """
    🧪 TEMPORARY ADMIN TEST TOOL — NOT a real user-facing feature.

    Lets an admin manually verify end-to-end that a teacher actually gets
    paid, for all 4 payment methods, without waiting for a session's real
    scheduled time to pass. Finds every not-yet-completed TutoringSession row
    for this booking and marks each one completed by calling the exact same
    `complete_session` logic as POST /sessions/{id}/complete (imported and
    called directly, not re-implemented) — so wallet crediting is identical
    to production. Requires the booking to already be 'confirmed' (i.e. the
    teacher — or, for manual rails, the admin — already accepted it), the
    same precondition complete_session itself enforces per-session.
    """
    from app.routers.sessions import complete_session

    booking = db.get(Booking, booking_id)
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.status != "confirmed":
        raise HTTPException(
            status_code=409,
            detail=f"Booking must be 'confirmed' (accepted) before simulating completion — currently '{booking.status}'.",
        )

    sessions = db.exec(
        select(TutoringSession)
        .where(TutoringSession.booking_id == booking_id)
        .where(TutoringSession.status.notin_(["completed", "cancelled"]))
    ).all()
    if not sessions:
        raise HTTPException(status_code=409, detail="No pending session found for this booking to complete.")

    # get_admin_user's admin sessions have no profiles.id (env-var credentials,
    # not a real user row) — complete_session only uses this id for the
    # teacher-ownership check, which role=="admin" already bypasses, so any
    # syntactically-valid UUID works here.
    admin_ctx = {"id": admin.get("id") or str(booking.teacher_id), "role": "admin"}
    results = []
    for s in sessions:
        result = await complete_session(session_id=s.id, current_user=admin_ctx, db=db)
        results.append(result)

    logger.info("🧪 [TEST] Simulated completion: booking_id=%s sessions=%d", booking_id, len(results))
    return {"booking_id": str(booking_id), "sessions_completed": len(results), "results": results}
