from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, select

from app.dependencies import get_admin_user, get_db
from app.models.booking import Booking
from app.models.notification import Notification
from app.models.profile import Profile

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Admin — Bookings"])


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
            subject=b.subject,
            comment=b.comment,
            status=b.status,
            created_at=b.created_at.isoformat(),
        ))
    return result


@router.post("/{booking_id}/approve-cash-payment")
async def approve_cash_payment(
    booking_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """
    Admin approves a cash payment:
    - confirms the booking (status → confirmed)
    - notifies student and teacher via in-app notification
    """
    booking = db.get(Booking, booking_id)
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.payment_method != "cash":
        raise HTTPException(status_code=400, detail="This booking did not use cash payment")
    if booking.status not in ("pending",):
        raise HTTPException(status_code=409, detail=f"Cannot approve booking with status '{booking.status}'")

    booking.status = "confirmed"
    db.add(booking)

    student = db.get(Profile, booking.student_id)
    teacher = db.get(Profile, booking.teacher_id)

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
            "✅ Paiement en espèces confirmé",
            f"Votre paiement pour la réservation du {booking.booking_date} a été validé. Le professeur va confirmer votre demande.",
        )
    if teacher:
        _notify(
            booking.teacher_id,
            "📬 Nouvelle demande de réservation",
            f"Un élève a réservé une séance pour le {booking.booking_date}. Veuillez accepter ou refuser.",
        )

    db.commit()
    logger.info("Cash payment approved: booking_id=%s", booking_id)
    return {"status": "confirmed", "booking_id": str(booking_id)}


@router.post("/{booking_id}/reject-cash-payment")
async def reject_cash_payment(
    booking_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Admin rejects (cancels) a cash payment booking."""
    booking = db.get(Booking, booking_id)
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.payment_method != "cash":
        raise HTTPException(status_code=400, detail="This booking did not use cash payment")
    if booking.status not in ("pending",):
        raise HTTPException(status_code=409, detail=f"Cannot reject booking with status '{booking.status}'")

    booking.status = "cancelled"
    db.add(booking)

    student = db.get(Profile, booking.student_id)
    if student:
        db.add(Notification(
            user_id=booking.student_id,
            title="❌ Paiement en espèces non validé",
            body=f"Votre paiement en espèces pour la réservation du {booking.booking_date} n'a pas pu être validé. Contactez le support.",
            type="booking",
            data={"booking_id": str(booking.id)},
        ))

    db.commit()
    return {"status": "cancelled", "booking_id": str(booking_id)}
