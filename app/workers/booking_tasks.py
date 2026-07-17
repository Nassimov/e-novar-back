from __future__ import annotations

"""
Celery tasks enforcing the booking response-time SLA (see
docs/migrations/067_booking_safety_rules.sql for the full rule write-up):

- task_auto_cancel_unanswered_bookings: a paid-and-authorized booking a
  teacher hasn't accepted/refused within platform_settings
  .booking_teacher_response_hours gets auto-cancelled, the student is never
  charged, the teacher gets a no-response strike with an escalating,
  self-expiring suspension, and every admin is notified.
- task_reinstate_expired_teacher_suspensions: lifts a suspension once its
  suspended_until has passed (only ever set by the task above — manual admin
  suspensions leave suspended_until NULL and are never auto-lifted).
"""

import logging
from typing import Any, Dict

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task
def task_auto_cancel_unanswered_bookings() -> Dict[str, int]:
    from datetime import datetime, timedelta

    from sqlmodel import Session, select

    from app.database import get_engine
    from app.models.admin import PlatformSettings
    from app.models.booking import Booking
    from app.models.profile import Profile, TeacherProfile, UserRole
    from app.services import notification as notif
    from app.services.booking_safety import apply_cancellation_side_effects

    cancelled = 0
    unpaid_expired = 0
    engine = get_engine()

    with Session(engine) as db:
        settings_row = db.get(PlatformSettings, True)
        timeout_hours = settings_row.booking_teacher_response_hours if settings_row else 24
        suspension_days_list = (
            settings_row.booking_no_response_suspension_days if settings_row and settings_row.booking_no_response_suspension_days else [2, 5, 10]
        )
        reset_days = settings_row.booking_no_response_reset_days if settings_row else 60

        cutoff = datetime.utcnow() - timedelta(hours=timeout_hours)
        candidates = db.exec(
            select(Booking).where(
                Booking.status == "pending",
                Booking.payment_method.in_(["cib", "edahabia"]),
                Booking.created_at < cutoff,
            )
        ).all()

        for booking in candidates:
            # Confirm there's actually something for the teacher to have
            # responded to — an abandoned/never-completed checkout isn't a
            # "no response", there was nothing to accept.
            authorized = False
            if booking.payment_method == "edahabia":
                authorized = booking.chargily_paid_at is not None
            elif booking.payment_method == "cib" and booking.stripe_cs_id:
                try:
                    from app.services.stripe import get_checkout_session
                    session_data = get_checkout_session(booking.stripe_cs_id)
                    authorized = session_data.get("payment_intent_status") in ("requires_capture", "succeeded")
                    pi_id = session_data.get("payment_intent")
                    if pi_id:
                        booking.stripe_pi_id = pi_id
                except Exception:
                    logger.warning("Could not verify Stripe session for booking %s — skipping this cycle", booking.id)
                    continue  # transient Stripe error — try again next run, don't guess

            if not authorized:
                booking.status = "cancelled"
                booking.cancelled_reason = "payment_never_completed"
                db.add(booking)
                db.commit()
                unpaid_expired += 1
                continue

            # Release the auth hold — manual-capture, so nothing was ever charged.
            if booking.payment_method == "cib" and booking.stripe_pi_id:
                try:
                    from app.services.stripe import cancel_payment_intent
                    cancel_payment_intent(booking.stripe_pi_id)
                except Exception:
                    pass  # already expired/cancelled — fine either way

            booking.status = "cancelled"
            booking.cancelled_reason = "teacher_no_response"
            db.add(booking)
            db.commit()

            apply_cancellation_side_effects(db, booking, reason="teacher_no_response")

            if booking.payment_method == "edahabia" and booking.chargily_paid_at is not None:
                # Chargily has no refund API — already paid, needs a human.
                for ar in db.exec(select(UserRole).where(UserRole.role == "admin")).all():
                    notif.persist(
                        db, user_id=ar.user_id, type="system",
                        title="⚠️ Remboursement Edahabia manuel requis",
                        body=(
                            f"Réservation auto-annulée (professeur sans réponse) déjà payée en "
                            f"Edahabia ({booking.amount} DA) — remboursement manuel requis."
                        ),
                        data={"booking_id": str(booking.id)},
                    )

            notif.persist(
                db, user_id=booking.student_id, type="booking_cancelled_timeout",
                title="Réservation annulée automatiquement",
                body="Le professeur n'a pas répondu à temps à ta demande. Ta réservation a été annulée et tu n'as pas été débité·e.",
                data={"booking_id": str(booking.id)},
            )
            notif.push(
                db, user_id=booking.student_id,
                title="Réservation annulée",
                body="Le professeur n'a pas répondu à temps — tu n'as pas été débité·e.",
                data={"booking_id": str(booking.id)},
            )

            tp = db.get(TeacherProfile, booking.teacher_id)
            if tp is not None:
                now = datetime.utcnow()
                if tp.last_no_response_at is None or (now - tp.last_no_response_at).days > reset_days:
                    tp.no_response_strikes = 0
                tp.no_response_strikes += 1
                tp.last_no_response_at = now
                idx = min(tp.no_response_strikes - 1, len(suspension_days_list) - 1)
                days = suspension_days_list[idx]
                tp.status = "suspended"
                tp.suspended_until = now + timedelta(days=days)
                tp.suspension_reason = f"no_response_auto (récidive n°{tp.no_response_strikes})"
                db.add(tp)
                db.commit()

                notif.persist(
                    db, user_id=booking.teacher_id, type="teacher_suspended",
                    title="⚠️ Compte suspendu — réservation sans réponse",
                    body=(
                        f"Tu n'as pas répondu à une demande de réservation dans les {timeout_hours}h. "
                        f"Ton compte est suspendu {days} jour(s) (récidive n°{tp.no_response_strikes})."
                    ),
                    data={"booking_id": str(booking.id), "days": days},
                )

                teacher_profile_row = db.get(Profile, booking.teacher_id)
                teacher_label = teacher_profile_row.full_name if teacher_profile_row else str(booking.teacher_id)
                for ar in db.exec(select(UserRole).where(UserRole.role == "admin")).all():
                    notif.persist(
                        db, user_id=ar.user_id, type="teacher_no_response_admin_alert",
                        title="Professeur sans réponse à une réservation",
                        body=(
                            f"{teacher_label} n'a pas répondu dans les délais (récidive n°{tp.no_response_strikes}) "
                            f"— suspendu automatiquement {days} jour(s)."
                        ),
                        data={"booking_id": str(booking.id), "teacher_id": str(booking.teacher_id)},
                    )

            cancelled += 1

    logger.info(
        "task_auto_cancel_unanswered_bookings: cancelled=%d (no-strike unpaid expirations=%d)",
        cancelled, unpaid_expired,
    )
    return {"cancelled": cancelled, "unpaid_expired": unpaid_expired}


@celery_app.task
def task_reinstate_expired_teacher_suspensions() -> Dict[str, int]:
    from datetime import datetime

    from sqlmodel import Session, select

    from app.database import get_engine
    from app.models.profile import TeacherProfile
    from app.services import notification as notif

    reinstated = 0
    engine = get_engine()

    with Session(engine) as db:
        now = datetime.utcnow()
        expired = db.exec(
            select(TeacherProfile).where(
                TeacherProfile.status == "suspended",
                TeacherProfile.suspended_until.is_not(None),
                TeacherProfile.suspended_until <= now,
            )
        ).all()
        for tp in expired:
            tp.status = "approved"
            tp.suspended_until = None
            db.add(tp)
            db.commit()
            notif.persist(
                db, user_id=tp.user_id, type="teacher_reinstated",
                title="Compte réactivé",
                body="Ta suspension automatique est terminée — ton compte est de nouveau actif.",
                data={},
            )
            reinstated += 1

    logger.info("task_reinstate_expired_teacher_suspensions: reinstated=%d", reinstated)
    return {"reinstated": reinstated}


@celery_app.task
def task_detect_online_teacher_no_show() -> Dict[str, int]:
    """
    A confirmed (paid) online session whose teacher never joined the
    LiveKit room within platform_settings.online_no_show_grace_minutes of
    the scheduled start is the teacher's fault, full stop — the student is
    refunded 100% and the teacher gets the same escalating strike as a
    booking no-response (see task_auto_cancel_unanswered_bookings above).
    """
    from datetime import datetime, timedelta

    from sqlmodel import Session, select

    from app.database import get_engine
    from app.models.admin import PlatformSettings
    from app.models.booking import Booking, TutoringSession
    from app.models.profile import Profile, TeacherProfile, UserRole
    from app.services import notification as notif
    from app.services.booking_safety import apply_cancellation_side_effects
    from app.services.refunds import per_lesson_amount, refund_amount_for_booking

    flagged = 0
    engine = get_engine()

    with Session(engine) as db:
        settings_row = db.get(PlatformSettings, True)
        grace_minutes = settings_row.online_no_show_grace_minutes if settings_row else 20
        suspension_days_list = (
            settings_row.booking_no_response_suspension_days if settings_row and settings_row.booking_no_response_suspension_days else [2, 5, 10]
        )
        reset_days = settings_row.booking_no_response_reset_days if settings_row else 60
        now = datetime.utcnow()

        candidates = db.exec(
            select(TutoringSession).where(
                TutoringSession.mode == "online",
                TutoringSession.status.in_(["scheduled", "waiting", "live"]),
                TutoringSession.teacher_joined_at.is_(None),
            )
        ).all()

        for session in candidates:
            if now < session.scheduled_at + timedelta(minutes=grace_minutes):
                continue  # still inside the grace window

            booking = db.get(Booking, session.booking_id) if session.booking_id else None
            if booking is None or booking.status != "confirmed":
                continue  # never actually paid/accepted — nothing to refund or penalize

            refund_amt = per_lesson_amount(booking)
            session.status = "no_show"
            session.no_show = True
            session.cancellation_reason = "teacher_no_show"
            session.cancelled_at = now
            session.refund_percentage = 100
            session.refund_amount = refund_amt
            session.teacher_payout_amount = 0
            db.add(session)
            db.commit()

            refund_result = refund_amount_for_booking(
                db, booking, refund_amt,
                note="Professeur absent en ligne (no-show détecté automatiquement).",
            )
            apply_cancellation_side_effects(db, booking, reason="teacher_no_response")

            notif.persist(
                db, user_id=session.student_id, type="session_no_show",
                title="Séance non honorée par le professeur",
                body=(
                    "Le professeur ne s'est pas connecté à ta séance. Tu as été remboursé·e intégralement."
                    if refund_result["refunded"] else
                    "Le professeur ne s'est pas connecté à ta séance. Ton remboursement est en cours de traitement."
                ),
                data={"session_id": str(session.id)},
            )
            notif.push(
                db, user_id=session.student_id,
                title="Séance non honorée",
                body="Le professeur ne s'est pas connecté — tu es remboursé·e.",
                data={"session_id": str(session.id)},
            )

            tp = db.get(TeacherProfile, session.teacher_id)
            if tp is not None:
                if tp.last_no_response_at is None or (now - tp.last_no_response_at).days > reset_days:
                    tp.no_response_strikes = 0
                tp.no_response_strikes += 1
                tp.last_no_response_at = now
                idx = min(tp.no_response_strikes - 1, len(suspension_days_list) - 1)
                days = suspension_days_list[idx]
                tp.status = "suspended"
                tp.suspended_until = now + timedelta(days=days)
                tp.suspension_reason = f"teacher_session_no_show (récidive n°{tp.no_response_strikes})"
                db.add(tp)
                db.commit()

                notif.persist(
                    db, user_id=session.teacher_id, type="teacher_suspended",
                    title="⚠️ Compte suspendu — absence en séance",
                    body=(
                        f"Tu ne t'es pas connecté·e à une séance confirmée. Ton compte est suspendu "
                        f"{days} jour(s) (récidive n°{tp.no_response_strikes})."
                    ),
                    data={"session_id": str(session.id), "days": days},
                )

                teacher_profile_row = db.get(Profile, session.teacher_id)
                teacher_label = teacher_profile_row.full_name if teacher_profile_row else str(session.teacher_id)
                for ar in db.exec(select(UserRole).where(UserRole.role == "admin")).all():
                    notif.persist(
                        db, user_id=ar.user_id, type="teacher_no_show_admin_alert",
                        title="Professeur absent en séance en ligne",
                        body=(
                            f"{teacher_label} ne s'est pas connecté à une séance confirmée "
                            f"(récidive n°{tp.no_response_strikes}) — suspendu {days} jour(s)."
                        ),
                        data={"session_id": str(session.id), "teacher_id": str(session.teacher_id)},
                    )

            flagged += 1

    logger.info("task_detect_online_teacher_no_show: flagged=%d", flagged)
    return {"flagged": flagged}
