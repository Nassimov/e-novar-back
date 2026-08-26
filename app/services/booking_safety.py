from __future__ import annotations

"""
Shared side effects for a booking getting cancelled without the lesson
happening — whether the teacher explicitly refused it (see
app/routers/teachers.py::refuse_booking) or never responded in time (see
app/workers/booking_tasks.py). Both call apply_cancellation_side_effects()
so the two paths can never drift apart.

Also home to the severity-weighted strike/suspension helpers used by every
teacher- and student-fault path (see docs/migrations/069_fairness_pass.sql
for the full policy write-up):

  TEACHER_STRIKE_WEIGHTS — an explicit refusal is NOT in here and never
  strikes the account at all: refusing quickly and honestly is the *best*
  outcome after a request (far better than silence or a late cancellation),
  and auto-punishing it would perversely push teachers toward accepting
  requests they can't actually honor, or going silent instead of refusing.
  It still counts toward the same-slot 2-strikes block
  (apply_cancellation_side_effects), just not the account-level suspension.
    - no_response (ignored a booking request until timeout): weight 1
    - teacher_cancelled_confirmed (backed out after accepting): weight 2
    - teacher_no_show (never joined / reported absent from a paid, imminent
      lesson — the worst case, the student's time was actually wasted): weight 3

  STUDENT_STRIKE_WEIGHTS — mirrors the above for the one student-fault case
  that exists (no-show), but the consequence is deliberately different in
  kind, not just degree: a booking-only suspension (STUDENT_SUSPENSION
  below), never a full account lock — a paying customer keeps access to
  their history/messages/existing sessions throughout.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple
from uuid import UUID

from sqlmodel import Session, select

from app.models.booking import Booking, BookingRefusal, TutoringSession
from app.models.scheduling import TeacherSlot

logger = logging.getLogger(__name__)

TEACHER_STRIKE_WEIGHTS = {
    "no_response": 1,
    "teacher_cancelled_confirmed": 2,
    "teacher_no_show": 3,
}

STUDENT_STRIKE_WEIGHTS = {
    "student_no_show": 1,
}


def apply_cancellation_side_effects(db: Session, booking: Booking, reason: str) -> None:
    """
    reason: "teacher_refused" | "teacher_no_response"

    - Reopens the linked TeacherSlot (if any) so the freed seat becomes
      bookable again — safe for group slots too, since the capacity-aware
      claim in student_teachers.py._claim_slot_or_409 re-closes it once
      actually full again.
    - Cancels every TutoringSession row tied to this booking (status was
      still "scheduled" from creation-time — see student_teachers.py's
      book_teacher_slot — since nothing else ever touched it once the
      booking itself got rejected before acceptance). Without this, a
      refused/timed-out booking kept showing up as a real upcoming session
      to the student (only Booking.status was checked to gate the
      pending-vs-upcoming split in student_dashboard.py's /sessions list),
      even though the teacher never confirmed it.
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
    now_utc = datetime.utcnow()
    seen: set[tuple[Optional[UUID], int, object]] = set()
    for s in sessions:
        if s.status not in ("completed", "cancelled"):
            s.status = "cancelled"
            s.cancelled_at = now_utc
            s.cancellation_reason = reason
            s.refund_percentage = 100
            db.add(s)

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


def apply_teacher_strike(db: Session, teacher_id: UUID, reason: str, *, human_label: str) -> Tuple[int, int]:
    """
    Increments the teacher's severity-weighted strike counter (see
    TEACHER_STRIKE_WEIGHTS), resetting it first if the last incident was
    long enough ago (booking_no_response_reset_days), then applies/escalates
    the resulting self-expiring suspension. Notifies the teacher and all
    admins. Returns (new_strike_total, suspension_days_applied).

    Callers processing multiple TutoringSession rows for the SAME real-world
    incident (e.g. a group lesson with N enrolled students) must call this
    ONCE per incident, not once per row — see
    app/workers/booking_tasks.py's dedup-by-(teacher_id, scheduled_at) — a
    single missed class is one strike, not N.
    """
    from app.models.admin import PlatformSettings
    from app.models.profile import Profile, TeacherProfile, UserRole
    from app.services.notification_engine import emit

    weight = TEACHER_STRIKE_WEIGHTS.get(reason, 1)
    settings_row = db.get(PlatformSettings, True)
    suspension_days_list = (
        settings_row.booking_no_response_suspension_days
        if settings_row and settings_row.booking_no_response_suspension_days else [2, 5, 10]
    )
    reset_days = settings_row.booking_no_response_reset_days if settings_row else 60

    tp = db.get(TeacherProfile, teacher_id)
    if tp is None:
        return (0, 0)

    now = datetime.utcnow()
    if tp.last_no_response_at is None or (now - tp.last_no_response_at).days > reset_days:
        tp.no_response_strikes = 0
    tp.no_response_strikes += weight
    tp.last_no_response_at = now
    idx = min(tp.no_response_strikes - 1, len(suspension_days_list) - 1)
    days = suspension_days_list[idx]
    tp.status = "suspended"
    tp.suspended_until = now + timedelta(days=days)
    tp.suspension_reason = f"{reason} (score {tp.no_response_strikes})"
    db.add(tp)
    db.commit()

    emit(
        db, event_type="teacher_suspended", user_id=teacher_id,
        title_override="⚠️ Compte suspendu",
        body_override=f"{human_label} Ton compte est suspendu {days} jour(s) (score de sévérité : {tp.no_response_strikes}).",
        data={"reason": reason, "days": days},
        dedup_key=f"teacher_suspended:{teacher_id}:{tp.no_response_strikes}",
    )
    teacher_profile_row = db.get(Profile, teacher_id)
    teacher_label = teacher_profile_row.full_name if teacher_profile_row else str(teacher_id)
    for ar in db.exec(select(UserRole).where(UserRole.role == "admin")).all():
        emit(
            db, event_type="teacher_strike_admin_alert", user_id=ar.user_id,
            title_override="Sanction automatique appliquée à un professeur",
            body_override=f"{teacher_label} — {human_label} (score {tp.no_response_strikes}) — suspendu {days} jour(s).",
            data={"teacher_id": str(teacher_id), "reason": reason, "days": days},
            dedup_key=f"teacher_strike_admin_alert:{teacher_id}:{tp.no_response_strikes}:{ar.user_id}",
        )
    return (tp.no_response_strikes, days)


def apply_student_strike(db: Session, student_id: UUID, reason: str, *, human_label: str) -> Tuple[int, int]:
    """
    Student-side mirror of apply_teacher_strike — same escalating logic, but
    the consequence is a booking-only suspension (StudentProfile
    .booking_suspended_until), never a full account lock. See
    app/routers/student_teachers.py's booking-creation gate for enforcement.
    Returns (new_strike_total, suspension_days_applied).
    """
    from app.models.admin import PlatformSettings
    from app.models.profile import Profile, StudentProfile, UserRole
    from app.services.notification_engine import emit

    weight = STUDENT_STRIKE_WEIGHTS.get(reason, 1)
    settings_row = db.get(PlatformSettings, True)
    suspension_days_list = (
        settings_row.student_no_show_suspension_days
        if settings_row and settings_row.student_no_show_suspension_days else [3, 7, 14]
    )
    reset_days = settings_row.student_no_show_reset_days if settings_row else 60

    sp = db.get(StudentProfile, student_id)
    if sp is None:
        return (0, 0)

    now = datetime.utcnow()
    if sp.last_no_show_at is None or (now - sp.last_no_show_at).days > reset_days:
        sp.no_show_strikes = 0
    sp.no_show_strikes += weight
    sp.last_no_show_at = now

    # First offense: warning only, no booking restriction yet — see the
    # policy write-up in docs/migrations/069_fairness_pass.sql.
    days = 0
    if sp.no_show_strikes > 1:
        idx = min(sp.no_show_strikes - 2, len(suspension_days_list) - 1)
        days = suspension_days_list[idx]
        sp.booking_suspended_until = now + timedelta(days=days)
        sp.booking_suspension_reason = f"{reason} (score {sp.no_show_strikes})"
    db.add(sp)
    db.commit()

    if days > 0:
        emit(
            db, event_type="student_booking_suspended", user_id=student_id,
            title_override="⚠️ Réservations temporairement bloquées",
            body_override=f"{human_label} Tu ne peux plus créer de nouvelle réservation pendant {days} jour(s).",
            data={"reason": reason, "days": days},
            dedup_key=f"student_booking_suspended:{student_id}:{sp.no_show_strikes}",
        )
    else:
        emit(
            db, event_type="student_no_show_warning", user_id=student_id,
            title_override="Absence constatée",
            body_override=f"{human_label} En cas de récidive, tes réservations pourront être temporairement bloquées.",
            data={"reason": reason},
            dedup_key=f"student_no_show_warning:{student_id}:{sp.no_show_strikes}",
        )
    student_profile_row = db.get(Profile, student_id)
    student_label = student_profile_row.full_name if student_profile_row else str(student_id)
    for ar in db.exec(select(UserRole).where(UserRole.role == "admin")).all():
        emit(
            db, event_type="student_no_show_admin_alert", user_id=ar.user_id,
            title_override="Absence élève constatée",
            body_override=f"{student_label} — {human_label} (score {sp.no_show_strikes})" + (f" — réservations bloquées {days} jour(s)." if days else " — avertissement envoyé."),
            data={"student_id": str(student_id), "reason": reason, "days": days},
            dedup_key=f"student_no_show_admin_alert:{student_id}:{sp.no_show_strikes}:{ar.user_id}",
        )

    # A linked parent (accepted link — see app/models/parent_link.py) is
    # otherwise completely blind to their child's no-shows/suspensions: every
    # other surface (app/routers/parent.py's session list) shows the raw
    # status with no explanation. Every incident is reported to the parent,
    # not just ones that escalate to a suspension — they're the one actually
    # responsible for getting their child to a paid lesson on time.
    from app.models.parent_link import ParentStudentLink
    parent_links = db.exec(
        select(ParentStudentLink).where(
            ParentStudentLink.student_id == student_id,
            ParentStudentLink.status == "accepted",
        )
    ).all()
    for link in parent_links:
        emit(
            db, event_type="child_no_show_alert", user_id=link.parent_id,
            title_override="Absence de votre enfant constatée",
            body_override=(
                f"{student_label} — {human_label}"
                + (f" Réservations bloquées {days} jour(s)." if days else " Avertissement envoyé — récidive = blocage temporaire des réservations.")
            ),
            data={"student_id": str(student_id), "reason": reason, "days": days},
            dedup_key=f"child_no_show_alert:{student_id}:{sp.no_show_strikes}:{link.parent_id}",
        )

    return (sp.no_show_strikes, days)
