from __future__ import annotations

"""
Shared side effects for a booking getting cancelled without the lesson
happening — whether the teacher explicitly refused it (see
app/routers/teachers.py::refuse_booking) or never responded in time (see
app/workers/booking_tasks.py). Both call apply_cancellation_side_effects()
so the two paths can never drift apart.
"""

import logging
from typing import Optional
from uuid import UUID

from sqlmodel import Session, select

from app.models.booking import Booking, BookingRefusal, TutoringSession
from app.models.scheduling import TeacherSlot

logger = logging.getLogger(__name__)


def apply_cancellation_side_effects(db: Session, booking: Booking, reason: str) -> None:
    """
    reason: "teacher_refused" | "teacher_no_response"

    - Reopens the linked TeacherSlot (if any) so the freed seat becomes
      bookable again — safe for group slots too, since the capacity-aware
      claim in student_teachers.py._claim_slot_or_409 re-closes it once
      actually full again.
    - Records one BookingRefusal row per distinct (subject, weekday, time)
      combo among this booking's TutoringSession rows (a pack can span
      several) — this is what student_teachers.py._check_refusal_block
      counts against the 2-strikes rule.
    """
    if booking.slot_id:
        slot = db.get(TeacherSlot, booking.slot_id)
        if slot is not None and slot.status == "booked":
            slot.status = "open"
            db.add(slot)

    sessions = db.exec(
        select(TutoringSession).where(TutoringSession.booking_id == booking.id)
    ).all()
    seen: set[tuple[Optional[UUID], int, object]] = set()
    for s in sessions:
        key = (s.subject_id, s.scheduled_at.weekday(), s.scheduled_at.time())
        if key in seen:
            continue
        seen.add(key)
        db.add(BookingRefusal(
            student_id=booking.student_id,
            teacher_id=booking.teacher_id,
            subject_id=s.subject_id,
            weekday=s.scheduled_at.weekday(),
            slot_time=s.scheduled_at.time(),
            booking_id=booking.id,
            reason=reason,
        ))
    db.commit()
    logger.info(
        "Booking cancellation side effects applied: booking_id=%s reason=%s strikes_recorded=%d",
        booking.id, reason, len(seen),
    )
