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
    from datetime import datetime, timedelta, timezone

    from sqlmodel import Session, select

    from app.database import get_engine
    from app.models.admin import PlatformSettings
    from app.models.booking import Booking
    from app.models.profile import UserRole
    from app.services.notification_engine import emit
    from app.services.booking_safety import apply_cancellation_side_effects, apply_teacher_strike

    cancelled = 0
    unpaid_expired = 0
    engine = get_engine()

    with Session(engine) as db:
        settings_row = db.get(PlatformSettings, True)
        timeout_hours = settings_row.booking_teacher_response_hours if settings_row else 24

        cutoff = datetime.now(timezone.utc) - timedelta(hours=timeout_hours)
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
                    emit(
                        db, event_type="system", user_id=ar.user_id,
                        title_i18n={
                            "fr": "⚠️ Remboursement Edahabia manuel requis",
                            "en": "⚠️ Manual Edahabia refund required",
                            "ar": "⚠️ يتطلب استرجاع يدوي عبر Edahabia",
                            "tm": "⚠️ Yesra tuɣalin s ufus s Edahabia",
                        },
                        body_i18n={
                            "fr": (
                                f"Réservation auto-annulée (professeur sans réponse) déjà payée en "
                                f"Edahabia ({booking.amount} DA) — remboursement manuel requis."
                            ),
                            "en": (
                                f"Auto-cancelled booking (teacher didn't respond) already paid via "
                                f"Edahabia ({booking.amount} DZD) — manual refund required."
                            ),
                            "ar": (
                                f"حجز أُلغي تلقائيًا (الأستاذ لم يرد) وتم دفعه مسبقًا عبر Edahabia "
                                f"({booking.amount} دج) — يتطلب استرجاع يدوي."
                            ),
                            "tm": (
                                f"Aḥerz yettwasefsex s wudem awurman (aselmad ur d-yerri ara) yettwaxlaṣ yakan s "
                                f"Edahabia ({booking.amount} DA) — yesra tuɣalin s ufus."
                            ),
                        },
                        data={"booking_id": str(booking.id)},
                        dedup_key=f"edahabia_refund_needed:{booking.id}:{ar.user_id}",
                    )

            emit(
                db, event_type="booking_cancelled_timeout", user_id=booking.student_id,
                title_i18n={
                    "fr": "Réservation annulée automatiquement",
                    "en": "Booking automatically cancelled",
                    "ar": "تم إلغاء الحجز تلقائيًا",
                    "tm": "Aḥerz yettwasefsex s wudem awurman",
                },
                body_i18n={
                    "fr": "Le professeur n'a pas répondu à temps à ta demande. Ta réservation a été annulée et tu n'as pas été débité·e.",
                    "en": "The teacher didn't respond to your request in time. Your booking was cancelled and you were not charged.",
                    "ar": "لم يرد الأستاذ على طلبك في الوقت المحدد. تم إلغاء حجزك ولم يتم خصم أي مبلغ منك.",
                    "tm": "Aselmad ur d-yerri ara ɣef unadi-inek/inem deg lawan. Aḥerz-ik/inem yettwasefsex, ur ak/akem-nekkis idrimen.",
                },
                data={"booking_id": str(booking.id)},
                dedup_key=f"booking_cancelled_timeout:{booking.id}",
            )

            apply_teacher_strike(
                db, booking.teacher_id, "no_response",
                human_label_i18n={
                    "fr": f"Tu n'as pas répondu à une demande de réservation dans les {timeout_hours}h.",
                    "en": f"You didn't respond to a booking request within {timeout_hours}h.",
                    "ar": f"لم ترد على طلب حجز خلال {timeout_hours} ساعة.",
                    "tm": f"Ur d-terriḍ ara ɣef unadi n uḥerz deg {timeout_hours}h.",
                },
            )

            cancelled += 1

    logger.info(
        "task_auto_cancel_unanswered_bookings: cancelled=%d (no-strike unpaid expirations=%d)",
        cancelled, unpaid_expired,
    )
    return {"cancelled": cancelled, "unpaid_expired": unpaid_expired}


@celery_app.task
def task_reinstate_expired_teacher_suspensions() -> Dict[str, int]:
    from datetime import datetime, timezone

    from sqlmodel import Session, select

    from app.database import get_engine
    from app.models.profile import TeacherProfile
    from app.services.notification_engine import emit

    reinstated = 0
    engine = get_engine()

    with Session(engine) as db:
        now = datetime.now(timezone.utc)
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
            emit(
                db, event_type="teacher_reinstated", user_id=tp.user_id,
                title_i18n={
                    "fr": "Compte réactivé",
                    "en": "Account reinstated",
                    "ar": "تمت إعادة تفعيل الحساب",
                    "tm": "Amiḍan yuɣal-d",
                },
                body_i18n={
                    "fr": "Ta suspension automatique est terminée — ton compte est de nouveau actif.",
                    "en": "Your automatic suspension has ended — your account is active again.",
                    "ar": "انتهى إيقافك التلقائي — حسابك نشط من جديد.",
                    "tm": "Aḥbas-ik/inem awurman yekfa — amiḍan-ik/inem yuɣal-d yermed.",
                },
                dedup_key=f"teacher_reinstated:{tp.user_id}:{now.date()}",
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
    from datetime import datetime, timedelta, timezone

    from sqlmodel import Session, select

    from app.database import get_engine
    from app.models.admin import PlatformSettings
    from app.models.booking import Booking, TutoringSession
    from app.services.notification_engine import emit
    from app.services.booking_safety import apply_cancellation_side_effects, apply_teacher_strike
    from app.services.refunds import per_lesson_amount, refund_amount_for_booking

    flagged = 0
    # A group lesson has one TutoringSession row per enrolled student, all
    # sharing (teacher_id, scheduled_at) — refund+notify happens per row
    # below (each student individually deserves that), but the STRIKE must
    # only apply once per real-world incident, or one missed group class of
    # N students would wrongly register as N strikes.
    struck_incidents = set()
    engine = get_engine()

    with Session(engine) as db:
        settings_row = db.get(PlatformSettings, True)
        grace_minutes = settings_row.online_no_show_grace_minutes if settings_row else 20
        now = datetime.now(timezone.utc)

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

            body_i18n = (
                {
                    "fr": "Le professeur ne s'est pas connecté à ta séance. Tu as été remboursé·e intégralement.",
                    "en": "The teacher didn't join your lesson. You were fully refunded.",
                    "ar": "لم يتصل الأستاذ بحصتك. تم استرجاع كامل المبلغ.",
                    "tm": "Aselmad ur d-yeqqin ara ɣer tiɣimit-ik/inem. Tettwarreḍ-d s lekmal.",
                }
                if refund_result["refunded"] else
                {
                    "fr": "Le professeur ne s'est pas connecté à ta séance. Ton remboursement est en cours de traitement.",
                    "en": "The teacher didn't join your lesson. Your refund is being processed.",
                    "ar": "لم يتصل الأستاذ بحصتك. جارٍ معالجة استرجاع مبلغك.",
                    "tm": "Aselmad ur d-yeqqin ara ɣer tiɣimit-ik/inem. Tuɣalin n idrimen-ik/inem tettwaxdem tura.",
                }
            )
            emit(
                db, event_type="session_no_show", user_id=session.student_id,
                title_i18n={
                    "fr": "Séance non honorée par le professeur",
                    "en": "Lesson not honored by the teacher",
                    "ar": "لم يلتزم الأستاذ بالحصة",
                    "tm": "Tiɣimit ur tettwaḍfar ara sɣur uselmad",
                },
                body_i18n=body_i18n,
                data={"session_id": str(session.id)},
                dedup_key=f"session_no_show:{session.id}",
            )

            incident_key = (session.teacher_id, session.scheduled_at)
            if incident_key not in struck_incidents:
                struck_incidents.add(incident_key)
                apply_teacher_strike(
                    db, session.teacher_id, "teacher_no_show",
                    human_label_i18n={
                        "fr": "Tu ne t'es pas connecté·e à une séance confirmée.",
                        "en": "You didn't join a confirmed lesson.",
                        "ar": "لم تتصل بحصة مؤكدة.",
                        "tm": "Ur d-teqqineḍ ara ɣer tiɣimit yettwasenteḍen.",
                    },
                )

            flagged += 1

    logger.info("task_detect_online_teacher_no_show: flagged=%d", flagged)
    return {"flagged": flagged}


@celery_app.task
def task_detect_online_student_no_show() -> Dict[str, int]:
    """
    A confirmed online session where the TEACHER showed up but the student
    never joined within the grace window is the student's fault, not the
    teacher's — symmetric opposite of task_detect_online_teacher_no_show
    above, with deliberately different consequences: the teacher is paid
    normally (they showed up, ready to teach — see credit_session_payout,
    called directly here instead of going through the usual multi-factor
    trust score, since we already have stronger evidence than that flow was
    designed to require), the student is NOT refunded, and repeated
    student no-shows escalate a booking-only suspension
    (apply_student_strike) — never a full account lock.
    """
    from datetime import datetime, timedelta, timezone

    from sqlmodel import Session, select

    from app.database import get_engine
    from app.models.admin import PlatformSettings
    from app.models.booking import Booking, TutoringSession
    from app.models.session_validation import SessionValidation
    from app.services.notification_engine import emit
    from app.services.booking_safety import apply_student_strike
    from app.services.session_validation import credit_session_payout

    flagged = 0
    engine = get_engine()

    with Session(engine) as db:
        settings_row = db.get(PlatformSettings, True)
        grace_minutes = settings_row.online_no_show_grace_minutes if settings_row else 20
        now = datetime.now(timezone.utc)

        candidates = db.exec(
            select(TutoringSession).where(
                TutoringSession.mode == "online",
                TutoringSession.status.in_(["scheduled", "waiting", "live"]),
                TutoringSession.teacher_joined_at.is_not(None),
                TutoringSession.student_joined_at.is_(None),
            )
        ).all()

        for session in candidates:
            if now < session.scheduled_at + timedelta(minutes=grace_minutes):
                continue  # still inside the grace window

            booking = db.get(Booking, session.booking_id) if session.booking_id else None
            if booking is None or booking.status != "confirmed":
                continue  # never actually paid/accepted

            sv = db.exec(
                select(SessionValidation).where(SessionValidation.session_id == session.id)
            ).first()
            if sv is None:
                continue  # one SessionValidation row per session, created alongside it at booking time

            session.cancellation_reason = "student_no_show"
            session.no_show = True
            db.add(session)
            credit_session_payout(db, session, sv)  # pays the teacher, sets status="completed"
            db.commit()

            emit(
                db, event_type="session_student_no_show", user_id=session.teacher_id,
                title_i18n={
                    "fr": "Élève absent",
                    "en": "Student absent",
                    "ar": "غياب التلميذ",
                    "tm": "Anelmad ur d-yusi ara",
                },
                body_i18n={
                    "fr": "L'élève ne s'est pas connecté à la séance. Tu es payé·e normalement — ce n'est pas ta faute.",
                    "en": "The student didn't join the lesson. You are paid as usual — this isn't your fault.",
                    "ar": "لم يتصل التلميذ بالحصة. تتقاضى أجرك كالعادة — ليس هذا خطأك.",
                    "tm": "Anelmad ur d-yeqqin ara ɣer tiɣimit. Tettwaxelseḍ akken tettwalin — mačči d ddnub-ik/inem.",
                },
                data={"session_id": str(session.id)},
                dedup_key=f"session_student_no_show:{session.id}",
            )

            apply_student_strike(
                db, session.student_id, "student_no_show",
                human_label_i18n={
                    "fr": "Tu ne t'es pas connecté·e à une séance confirmée.",
                    "en": "You didn't join a confirmed lesson.",
                    "ar": "لم تتصل بحصة مؤكدة.",
                    "tm": "Ur d-teqqineḍ ara ɣer tiɣimit yettwasenteḍen.",
                },
            )

            flagged += 1

    logger.info("task_detect_online_student_no_show: flagged=%d", flagged)
    return {"flagged": flagged}


@celery_app.task
def task_auto_resolve_disputes() -> Dict[str, int]:
    """
    Resolves a structured in-person absence report (dispute_reason_code —
    see app/routers/session_validation.py's dispute_session) in the filer's
    favor once in_person_dispute_auto_resolve_hours has passed with no
    counter-dispute from the other party. Same financial/strike consequences
    as the automatic online detectors above — this is the manual-report
    equivalent for at_home/at_student sessions, which have no join-timestamp
    signal to detect absence automatically.
    """
    from datetime import datetime, timezone

    from sqlmodel import Session, select

    from app.database import get_engine
    from app.models.booking import Booking
    from app.models.session_validation import SessionValidation
    from app.services.notification_engine import emit
    from app.services.booking_safety import apply_student_strike, apply_teacher_strike
    from app.services.refunds import per_lesson_amount, refund_amount_for_booking
    from app.services.session_validation import credit_session_payout

    resolved = 0
    # Same dedup need as task_detect_online_teacher_no_show: a group in-person
    # lesson has one SessionValidation row per enrolled student, all sharing
    # (teacher_id, scheduled_at) — if several students each file their own
    # "teacher_absent" report for the same missed class, that's still ONE
    # real-world incident and must only strike the teacher once. Refund
    # happens per row regardless (each student individually deserves theirs).
    struck_incidents = set()
    engine = get_engine()

    with Session(engine) as db:
        now = datetime.now(timezone.utc)
        candidates = db.exec(
            select(SessionValidation).where(
                SessionValidation.status == "disputed",
                SessionValidation.dispute_reason_code.is_not(None),
                SessionValidation.dispute_auto_resolve_at.is_not(None),
                SessionValidation.dispute_auto_resolve_at <= now,
                SessionValidation.dispute_countered_at.is_(None),
            )
        ).all()

        for sv in candidates:
            from app.models.booking import TutoringSession
            session = db.get(TutoringSession, sv.session_id)
            booking = db.get(Booking, sv.booking_id) if sv.booking_id else None
            if session is None:
                continue
            if booking is None or booking.status != "confirmed":
                # Defense in depth — dispute_session already rejects filing a
                # structured report on a non-confirmed booking, so this
                # shouldn't be reachable, but never auto-resolve a
                # payout/refund/strike against a booking nobody ever confirmed.
                sv.status = "admin_review"
                sv.updated_at = now
                db.add(sv)
                db.commit()
                continue

            # GPS is otherwise pure positive evidence (trust-score bonus only,
            # see app/services/session_validation.py) — this is the one place
            # it's used ACTIVELY: if both parties' GPS puts them at the same
            # place around the session, that directly contradicts an absence
            # claim from either side. A silent "nobody countered" default
            # shouldn't win against real evidence the other party was there —
            # force a human decision instead of auto-resolving.
            if sv.gps_teacher_lat is not None and sv.gps_student_lat is not None:
                from app.models.admin import PlatformSettings
                from app.services.session_validation import _haversine_meters

                settings_row = db.get(PlatformSettings, True)
                gps_threshold = settings_row.gps_proximity_threshold_meters if settings_row else 500
                dist = _haversine_meters(
                    sv.gps_teacher_lat, sv.gps_teacher_lng, sv.gps_student_lat, sv.gps_student_lng,
                )
                if dist <= gps_threshold:
                    sv.status = "admin_review"
                    sv.admin_review_note = (
                        "Auto-résolution bloquée : les positions GPS des deux parties concordent "
                        f"(~{round(dist)}m d'écart), ce qui contredit le signalement d'absence."
                    )
                    sv.updated_at = now
                    db.add(sv)
                    db.commit()
                    from app.models.profile import UserRole
                    for ar in db.exec(select(UserRole).where(UserRole.role == "admin")).all():
                        emit(
                            db, event_type="dispute_gps_contradiction_admin_alert", user_id=ar.user_id,
                            title_i18n={
                                "fr": "Litige contredit par le GPS — décision requise",
                                "en": "Dispute contradicted by GPS — decision required",
                                "ar": "تناقض في النزاع بحسب GPS — يتطلب قرارًا",
                                "tm": "Amennuɣ yemgirred akked GPS — yesra tasuret",
                            },
                            body_i18n={
                                "fr": sv.admin_review_note or "",
                                "en": f"Both parties' GPS positions match (~{round(dist)}m apart), contradicting the absence report.",
                                "ar": f"مواقع GPS للطرفين متطابقة (~{round(dist)} م فرق)، ما يناقض بلاغ الغياب.",
                                "tm": f"Imukan GPS n snat n yiḍfaren mṣadan (~{round(dist)}m gar-asen), ayagi yemgirred akked uneqqis n tuɣalin.",
                            },
                            data={"session_id": str(session.id)},
                            dedup_key=f"dispute_gps_contradiction:{sv.id}:{ar.user_id}",
                        )
                    continue

            if sv.dispute_reason_code == "student_absent":
                # Teacher showed up, student didn't — paid normally, student strike.
                sv.status = "approved"
                sv.admin_decision = "auto_resolved_student_absent"
                sv.admin_reviewed_at = now
                session.cancellation_reason = "student_no_show"
                session.no_show = True
                db.add(session)
                db.add(sv)
                credit_session_payout(db, session, sv)
                db.commit()

                emit(
                    db, event_type="session_student_no_show", user_id=session.teacher_id,
                    title_i18n={
                        "fr": "Absence élève confirmée",
                        "en": "Student absence confirmed",
                        "ar": "تأكد غياب التلميذ",
                        "tm": "Tuɣalin n unelmad tettwasenteḍ",
                    },
                    body_i18n={
                        "fr": "Ton signalement n'a pas été contesté — tu es payé·e normalement.",
                        "en": "Your report was not contested — you are paid as usual.",
                        "ar": "لم يُعترض على بلاغك — تتقاضى أجرك كالعادة.",
                        "tm": "Aneqqis-ik/inem ur yettwanaḍar ara — tettwaxelseḍ akken tettwalin.",
                    },
                    data={"session_id": str(session.id)},
                    dedup_key=f"dispute_resolved_student_absent:{sv.id}",
                )
                apply_student_strike(
                    db, session.student_id, "student_no_show",
                    human_label_i18n={
                        "fr": "Une absence signalée par ton professeur n'a pas été contestée.",
                        "en": "An absence reported by your teacher was not contested.",
                        "ar": "لم يتم الاعتراض على غياب أبلغ عنه أستاذك.",
                        "tm": "Tuɣalin i d-yebbedd uselmad-ik ur tettwanaḍar ara.",
                    },
                )

            elif sv.dispute_reason_code == "teacher_absent":
                # Student showed up, teacher didn't — full refund, teacher strike.
                sv.status = "rejected"
                sv.admin_decision = "auto_resolved_teacher_absent"
                sv.admin_reviewed_at = now
                session.cancellation_reason = "teacher_no_show"
                session.no_show = True
                session.status = "no_show"
                db.add(session)
                db.add(sv)
                db.commit()

                if booking is not None:
                    refund_amt = per_lesson_amount(booking)
                    refund_amount_for_booking(
                        db, booking, refund_amt,
                        note="Absence du professeur confirmée (signalement non contesté).",
                    )
                emit(
                    db, event_type="session_no_show", user_id=session.student_id,
                    title_i18n={
                        "fr": "Absence professeur confirmée",
                        "en": "Teacher absence confirmed",
                        "ar": "تأكد غياب الأستاذ",
                        "tm": "Tuɣalin n uselmad tettwasenteḍ",
                    },
                    body_i18n={
                        "fr": "Ton signalement n'a pas été contesté — tu as été remboursé·e.",
                        "en": "Your report was not contested — you have been refunded.",
                        "ar": "لم يُعترض على بلاغك — تم استرجاع مبلغك.",
                        "tm": "Aneqqis-ik/inem ur yettwanaḍar ara — tettwarreḍ-d idrimen-ik/inem.",
                    },
                    data={"session_id": str(session.id)},
                    dedup_key=f"dispute_resolved_teacher_absent:{sv.id}",
                )
                incident_key = (session.teacher_id, session.scheduled_at)
                if incident_key not in struck_incidents:
                    struck_incidents.add(incident_key)
                    apply_teacher_strike(
                        db, session.teacher_id, "teacher_no_show",
                        human_label_i18n={
                            "fr": "Une absence signalée par ton élève n'a pas été contestée.",
                            "en": "An absence reported by your student was not contested.",
                            "ar": "لم يتم الاعتراض على غياب أبلغ عنه تلميذك.",
                            "tm": "Tuɣalin i d-yebbedd unelmad-ik ur tettwanaḍar ara.",
                        },
                    )

            resolved += 1

    logger.info("task_auto_resolve_disputes: resolved=%d", resolved)
    return {"resolved": resolved}


@celery_app.task
def task_expire_unconfirmed_manual_payments() -> Dict[str, int]:
    """
    cash/transfer/rib_cib/rib_edahabia bookings need an ADMIN to manually
    confirm or reject the payment (see app/routers/admin/bookings.py) before
    the teacher can even respond — nothing was auto-cancelling these if an
    admin simply never got to it, despite older UI copy promising a 48h
    window. This is never the teacher's fault (they never got a chance to
    accept/refuse), so unlike task_auto_cancel_unanswered_bookings, this
    never strikes anyone — it just frees the slot and flags admins, since an
    expired RIB/cash booking might mean a real transfer arrived and was
    simply never reconciled in time (a process failure worth checking), not
    necessarily that nothing was ever paid.
    """
    from datetime import datetime, timedelta, timezone

    from sqlmodel import Session, select

    from app.database import get_engine
    from app.models.admin import PlatformSettings
    from app.models.booking import Booking, TutoringSession
    from app.models.profile import UserRole
    from app.models.scheduling import TeacherSlot
    from app.services.notification_engine import emit

    expired = 0
    engine = get_engine()

    with Session(engine) as db:
        settings_row = db.get(PlatformSettings, True)
        expiry_hours = settings_row.manual_payment_expiry_hours if settings_row else 48
        cutoff = datetime.now(timezone.utc) - timedelta(hours=expiry_hours)

        candidates = db.exec(
            select(Booking).where(
                Booking.status == "pending",
                Booking.payment_method.in_(["cash", "transfer", "rib_cib", "rib_edahabia"]),
                Booking.created_at < cutoff,
            )
        ).all()

        for booking in candidates:
            booking.status = "cancelled"
            booking.cancelled_reason = "manual_payment_expired"
            db.add(booking)

            if booking.slot_id:
                slot = db.get(TeacherSlot, booking.slot_id)
                if slot is not None and slot.status == "booked":
                    slot.status = "open"
                    db.add(slot)

            # The linked TutoringSession row(s) were already materialized as
            # status="scheduled" at booking time — cancel them too, or they
            # keep showing up as real upcoming sessions to the student even
            # though the payment was never confirmed (see
            # app/services/booking_safety.py's apply_cancellation_side_effects
            # for the equivalent fix on the teacher-refusal/no-response paths).
            linked_sessions = db.exec(
                select(TutoringSession).where(TutoringSession.booking_id == booking.id)
            ).all()
            for s in linked_sessions:
                if s.status not in ("completed", "cancelled"):
                    s.status = "cancelled"
                    s.cancelled_at = datetime.now(timezone.utc)
                    s.cancellation_reason = "manual_payment_expired"
                    s.refund_percentage = 100
                    db.add(s)

            db.commit()

            emit(
                db, event_type="booking_cancelled_timeout", user_id=booking.student_id,
                title_i18n={
                    "fr": "Réservation annulée — paiement non confirmé",
                    "en": "Booking cancelled — payment unconfirmed",
                    "ar": "تم إلغاء الحجز — لم يتم تأكيد الدفع",
                    "tm": "Aḥerz yettwasefsex — axelaṣ ur yettwasenteḍ ara",
                },
                body_i18n={
                    "fr": (
                        f"Ton paiement {booking.payment_method} n'a pas été confirmé dans les délais. "
                        "Ta réservation a été annulée. Si tu as déjà envoyé le paiement, contacte le support."
                    ),
                    "en": (
                        f"Your {booking.payment_method} payment was not confirmed in time. "
                        "Your booking was cancelled. If you already sent the payment, contact support."
                    ),
                    "ar": (
                        f"لم يتم تأكيد دفعتك عبر {booking.payment_method} في الوقت المحدد. "
                        "تم إلغاء حجزك. إذا كنت قد أرسلت الدفعة بالفعل، تواصل مع الدعم."
                    ),
                    "tm": (
                        f"Axelaṣ-ik/inem {booking.payment_method} ur yettwasenteḍ ara deg lawan. "
                        "Aḥerz-ik/inem yettwasefsex. Ma yella teznḍ yakan axelaṣ, nermes tallalt."
                    ),
                },
                data={"booking_id": str(booking.id)},
                dedup_key=f"manual_payment_expired:{booking.id}",
            )
            for ar in db.exec(select(UserRole).where(UserRole.role == "admin")).all():
                emit(
                    db, event_type="manual_payment_expired_admin_alert", user_id=ar.user_id,
                    title_i18n={
                        "fr": "Paiement manuel expiré sans traitement",
                        "en": "Manual payment expired unprocessed",
                        "ar": "انتهت صلاحية دفعة يدوية دون معالجة",
                        "tm": "Axelaṣ s ufus yemmuger lawan war axeddim",
                    },
                    body_i18n={
                        "fr": (
                            f"Réservation {booking.id} ({booking.payment_method}, {booking.amount} DA) auto-annulée "
                            f"après {expiry_hours}h sans confirmation admin. Vérifiez qu'aucun virement réel n'a été reçu."
                        ),
                        "en": (
                            f"Booking {booking.id} ({booking.payment_method}, {booking.amount} DZD) auto-cancelled "
                            f"after {expiry_hours}h without admin confirmation. Check that no real transfer was received."
                        ),
                        "ar": (
                            f"تم إلغاء الحجز {booking.id} ({booking.payment_method}، {booking.amount} دج) تلقائيًا "
                            f"بعد {expiry_hours} ساعة دون تأكيد من الإدارة. تحقق من عدم استلام أي تحويل فعلي."
                        ),
                        "tm": (
                            f"Aḥerz {booking.id} ({booking.payment_method}, {booking.amount} DA) yettwasefsex s wudem awurman "
                            f"ticki {expiry_hours}h war asentem n unedbal. Senqed ma yella ulac azuzen n tidet i d-yewḍen."
                        ),
                    },
                    data={"booking_id": str(booking.id)},
                    dedup_key=f"manual_payment_expired_admin_alert:{booking.id}:{ar.user_id}",
                )

            expired += 1

    logger.info("task_expire_unconfirmed_manual_payments: expired=%d", expired)
    return {"expired": expired}
