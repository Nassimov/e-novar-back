"""Session validation & trust-score engine (migration 063 / spec: production
session-completion workflow, redesigned 2026-09-14 — migration 118). See
app/routers/session_validation.py for the HTTP surface.

Design summary
--------------
- A teacher can never single-handedly make themselves payment-eligible.
- The trust score is a weighted sum of independent signals, all weights and
  thresholds pulled from PlatformSettings — never hardcoded (spec point 19).
- No more code/token exchange (dropped 2026-09-14): the student validates
  with a single "valider" button, full stop. student_validation's signal in
  compute_trust_score below only ever cared WHETHER student_validated_at
  was set, never HOW — so this simplification changes nothing about the
  trust-score math itself.
- Group lessons (one TutoringSession/SessionValidation row per enrolled
  student, sharing a Booking.slot_id — see app.services.livekit_video's
  group_slot_id/group_sessions) no longer require the teacher to confirm
  each student one at a time. Once >= PlatformSettings.
  trust_group_validation_threshold_percent of the group has individually
  validated, group_confirm_and_finalize lets the teacher finalize payout
  for the WHOLE group in one action — including students who personally
  never clicked validate, on the theory that a healthy validation rate is
  itself sufficient evidence the class happened. If that threshold is
  never reached before student_validation_window_hours elapses, the
  teacher can instead call file_group_report, which reuses the existing
  per-session student_validation_neglect dispute path (fanned out to every
  still-unvalidated sibling) rather than inventing a parallel review
  mechanism — the admin review queue, approve/reject endpoints, and the
  resulting student strike all already exist and need no changes.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from sqlmodel import Session, select

from app.models.admin import PlatformSettings
from app.models.booking import Booking, TutoringSession
from app.models.session_validation import SessionValidation, SessionValidationAuditLog
from app.services.pricing import PACK_SIZES

# ─── Audit log ────────────────────────────────────────────────────────────────

def log_audit(
    db: Session,
    *,
    session_id: Optional[UUID],
    booking_id: Optional[UUID],
    actor_user_id: Optional[UUID],
    actor_ip: Optional[str],
    action: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    db.add(SessionValidationAuditLog(
        session_id=session_id,
        booking_id=booking_id,
        actor_user_id=actor_user_id,
        actor_ip=actor_ip,
        action=action,
        metadata_=metadata or {},
    ))


def _notify(
    db: Session, user_id: UUID, title_i18n: "str | Dict[str, str]", body_i18n: "str | Dict[str, str]",
    data: Optional[Dict[str, Any]] = None,
) -> None:
    """A handful of call sites in this module (see app/routers/
    session_validation.py's end_session/dispute_session) predate the
    title_i18n/body_i18n convention and still pass a plain French string.
    notification_engine.emit()'s own try/except swallows ANY exception
    (by design — a notification failure must never break the real
    request), which meant those specific calls have been silently sending
    NOTHING since day one: _render_i18n does `title_i18n.get(lang)`, and a
    plain str has no .get(), so it always raised, always got swallowed,
    with nothing visible anywhere pointing at it. Normalizing a bare
    string into a same-text-every-language dict here fixes that (real per-
    language copy for those specific call sites is a separate follow-up,
    out of scope for this fix) without having to hunt down and rewrite
    every existing call site right now."""
    def _as_i18n(v: "str | Dict[str, str]") -> Dict[str, str]:
        return v if isinstance(v, dict) else {"fr": v, "en": v, "ar": v, "tm": v}

    from app.services.notification_engine import emit
    emit(
        db, event_type="session_validation", user_id=user_id,
        title_i18n=_as_i18n(title_i18n), body_i18n=_as_i18n(body_i18n), data=data or {},
    )


# ─── Setup ────────────────────────────────────────────────────────────────────

def get_or_create_validation(db: Session, session: TutoringSession) -> SessionValidation:
    """Every TutoringSession gets exactly one SessionValidation row, created
    lazily the first time anything in this workflow touches it (normally at
    booking-acceptance time — see app.routers.teachers.accept_booking)."""
    sv = db.exec(
        select(SessionValidation).where(SessionValidation.session_id == session.id)
    ).first()
    if sv is None:
        sv = SessionValidation(
            session_id=session.id,
            booking_id=session.booking_id,
            student_id=session.student_id,
            teacher_id=session.teacher_id,
            status="scheduled",
        )
        db.add(sv)
        db.flush()
    return sv


# ─── Trust score ──────────────────────────────────────────────────────────────

def _haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6_371_000  # Earth radius, meters
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def compute_trust_score(
    sv: SessionValidation,
    session: TutoringSession,
    settings: PlatformSettings,
    *,
    teacher_has_clean_history: bool,
) -> Tuple[int, Dict[str, Any]]:
    """Weighted sum, normalized to 0-100 regardless of what the configured
    weights actually add up to (spec: weights configurable, never hardcoded;
    this function only hardcodes which *signals* exist, not their weight)."""
    weights = {
        "student_validation": settings.trust_weight_student_validation,
        "teacher_confirmation": settings.trust_weight_teacher_confirmation,
        "session_completed": settings.trust_weight_session_completed,
        "online_duration": settings.trust_weight_online_duration,
        "gps_proximity": settings.trust_weight_gps_proximity,
        "clean_history": settings.trust_weight_clean_history,
    }
    total_weight = sum(weights.values()) or 1

    earned: Dict[str, float] = {}
    earned["student_validation"] = weights["student_validation"] if sv.student_validated_at else 0
    earned["teacher_confirmation"] = weights["teacher_confirmation"] if sv.teacher_confirmed_at else 0
    earned["session_completed"] = weights["session_completed"] if sv.teacher_ended_at else 0

    if session.mode == "online":
        if sv.online_duration_min is not None and session.duration_min:
            ratio = min(1.0, sv.online_duration_min / session.duration_min)
            # Anomaly: a connection far shorter than the scheduled duration
            # (spec point 12 example: 90 min planned, 3 min connected) should
            # tank this signal, not just prorate it gently.
            ratio = 0.0 if ratio < 0.5 else ratio
            earned["online_duration"] = round(weights["online_duration"] * ratio, 2)
        else:
            earned["online_duration"] = 0
    else:
        # Not applicable to in-person — exclude it from the denominator
        # entirely rather than scoring it 0 (0 would unfairly punish
        # in-person sessions for a signal that can't exist for them).
        total_weight -= weights["online_duration"]
        earned["online_duration"] = None

    if sv.gps_consent and sv.gps_teacher_lat is not None and sv.gps_student_lat is not None:
        dist = _haversine_meters(sv.gps_teacher_lat, sv.gps_teacher_lng, sv.gps_student_lat, sv.gps_student_lng)
        earned["gps_proximity"] = weights["gps_proximity"] if dist <= settings.gps_proximity_threshold_meters else 0
    else:
        # GPS is opt-in (spec point 13) — absence must not penalize the score,
        # same treatment as online_duration for in-person sessions.
        total_weight -= weights["gps_proximity"]
        earned["gps_proximity"] = None

    earned["clean_history"] = weights["clean_history"] if teacher_has_clean_history else 0

    raw_total = sum(v for v in earned.values() if v is not None)
    score = round((raw_total / total_weight) * 100) if total_weight > 0 else 0
    score = max(0, min(100, score))

    breakdown = {
        "weights": weights,
        "earned": earned,
        "total_weight_used": total_weight,
        "score": score,
    }
    return score, breakdown


def teacher_has_clean_history(db: Session, teacher_id: UUID, lookback: int = 20) -> bool:
    """No admin-rejected or disputed session in this teacher's most recent
    `lookback` validations."""
    recent = db.exec(
        select(SessionValidation)
        .where(SessionValidation.teacher_id == teacher_id)
        .order_by(SessionValidation.created_at.desc())
        .limit(lookback)
    ).all()
    return not any(sv.status in ("disputed", "rejected") for sv in recent)


# ─── Payment crediting ────────────────────────────────────────────────────────

def credit_session_payout(db: Session, session: TutoringSession, sv: SessionValidation) -> int:
    """The only place a teacher's wallet is ever credited for a lesson.
    Idempotent — a session already payment_credited_at is a no-op (returns 0)
    rather than double-crediting.
    Commission (business audit, 2026-09-08): PlatformSettings.commission_percent
    is taken net-at-payout — the teacher never sees a "gross then clawed
    back" amount, and since a completed session can never be
    cancelled/refunded (see app/routers/sessions.py's cancel_session), a
    commission taken here is never at risk of needing to be reversed."""
    if sv.payment_credited_at is not None:
        return 0

    booking = db.get(Booking, session.booking_id) if session.booking_id else None
    if booking is None:
        return 0

    from app.services.pricing import get_platform_settings

    pack_size = PACK_SIZES.get(booking.formula, 1)
    gross = round(booking.amount / pack_size)
    commission_pct = get_platform_settings(db).commission_percent or 0
    commission = round(gross * commission_pct / 100) if commission_pct > 0 else 0
    payout = gross - commission

    session.status = "completed"
    if session.ended_at is None:
        session.ended_at = datetime.now(timezone.utc)
    session.teacher_payout_amount = payout
    session.platform_commission_amount = commission
    db.add(session)

    if payout > 0:
        from app.services.wallet import credit_wallet
        try:
            credit_wallet(
                session.teacher_id, payout, "session_payout", "Paiement séance validée", db,
                ref_type="session_payout", ref_id=session.id,
            )
        except ValueError:
            pass  # teacher profile not found — nothing to credit

    sv.payment_credited_at = datetime.now(timezone.utc)
    sv.updated_at = sv.payment_credited_at
    db.add(sv)

    # Student's advertised lesson-completion EP (business/EP audit,
    # 2026-09-08): booking.kp_reward is set from the teacher's profile at
    # booking time and shown to the student as "you'll earn N EP for this
    # lesson" — but nothing ever actually granted it. The only prior
    # mechanism was a DB trigger (handle_booking_completed, removed by
    # migration 109/110) keyed on bookings.status turning 'completed',
    # which no code path ever sets (bookings only ever reach pending/
    # confirmed/cancelled) — so it had never fired. Granted here instead,
    # at the point a session actually completes; ref_id is the SESSION
    # (not the booking) so a multi-lesson pack correctly grants once per
    # lesson, not once total.
    if booking.kp_reward > 0:
        from app.models.kp import KpSource
        from app.services.kp import award_kp
        award_kp(
            session.student_id, booking.kp_reward, KpSource.lesson,
            "Séance terminée", db,
            ref_type="booking_completed", ref_id=session.id,
        )

    log_audit(
        db, session_id=session.id, booking_id=session.booking_id,
        actor_user_id=None, actor_ip=None, action="payment_credited",
        metadata={"amount": payout, "gross": gross, "commission": commission},
    )
    _notify(
        db, session.teacher_id,
        {
            "fr": "💰 Paiement crédité",
            "en": "💰 Payment credited",
            "ar": "💰 تم إضافة الدفعة",
            "tm": "💰 Axelaṣ yettwarna",
        },
        {
            "fr": f"{payout} DA ont été ajoutés à votre solde pour une séance validée.",
            "en": f"{payout} DZD were added to your balance for a validated lesson.",
            "ar": f"تمت إضافة {payout} دج إلى رصيدك مقابل حصة تم التحقق منها.",
            "tm": f"{payout} DA ttwarnan ɣer usiḍen-ik/inem ɣef tiɣimit yettwasenteḍen.",
        },
        {"session_id": str(session.id)},
    )

    # A completed, paid session is the single biggest driver of both badge
    # catalogues (sessions_completed, subject_hours, hours_taught,
    # students_taught, ...) — checking right here, at the moment the
    # milestone is actually reached, is what lets the global celebration
    # (src/lib/badge-celebration.ts) fire on whatever page the user is on,
    # instead of only the next time they happen to open the badges page
    # (student_badges.py / teacher_badges.py still do that lazy check too,
    # as a safety net for badges whose condition isn't tied to a session).
    # Never allowed to block a real payout over a badge-engine bug.
    try:
        from app.services.badge_engine import check_and_unlock_badges
        check_and_unlock_badges(session.student_id, db)
    except Exception:
        pass
    try:
        from app.services.teacher_badge_engine import check_and_unlock_teacher_badges
        check_and_unlock_teacher_badges(session.teacher_id, db)
    except Exception:
        pass

    return payout


# ─── Eligibility decision ─────────────────────────────────────────────────────

def evaluate_and_finalize(
    db: Session,
    session: TutoringSession,
    sv: SessionValidation,
    settings: PlatformSettings,
) -> None:
    """Called right after the student validates (or after teacher confirms,
    whichever completes last) — computes the trust score and either
    auto-approves (crediting payment) or routes to admin_review."""
    clean = teacher_has_clean_history(db, sv.teacher_id)
    score, breakdown = compute_trust_score(sv, session, settings, teacher_has_clean_history=clean)
    sv.trust_score = score
    sv.trust_score_breakdown = breakdown

    if score >= settings.trust_auto_approve_threshold:
        sv.status = "approved"
        sv.payment_eligible_at = datetime.now(timezone.utc)
        db.add(sv)
        db.flush()
        credit_session_payout(db, session, sv)
        _notify(
            db, sv.teacher_id,
            {
                "fr": "✅ Séance approuvée",
                "en": "✅ Lesson approved",
                "ar": "✅ تمت الموافقة على الحصة",
                "tm": "✅ Tiɣimit tettwaqbel",
            },
            {
                "fr": "Votre séance a été validée automatiquement — paiement crédité.",
                "en": "Your lesson was automatically validated — payment credited.",
                "ar": "تم التحقق من حصتك تلقائيًا — تمت إضافة الدفعة.",
                "tm": "Tiɣimit-inek/inem tettwasenteḍ s wudem awurman — axelaṣ yettwarna.",
            },
        )
        log_audit(db, session_id=session.id, booking_id=session.booking_id, actor_user_id=None,
                  actor_ip=None, action="auto_approved", metadata={"trust_score": score})
    else:
        sv.status = "admin_review"
        db.add(sv)
        log_audit(db, session_id=session.id, booking_id=session.booking_id, actor_user_id=None,
                  actor_ip=None, action="routed_to_admin_review", metadata={"trust_score": score})
        _notify(
            db, sv.teacher_id,
            {
                "fr": "🕓 Séance en cours de vérification",
                "en": "🕓 Lesson under review",
                "ar": "🕓 الحصة قيد المراجعة",
                "tm": "🕓 Tiɣimit deg tuzzelt",
            },
            {
                "fr": "Votre séance nécessite une vérification supplémentaire avant le paiement.",
                "en": "Your lesson needs additional review before payment.",
                "ar": "تتطلب حصتك مراجعة إضافية قبل الدفع.",
                "tm": "Tiɣimit-inek/inem tesra tuzzelt niḍen uqbel axelaṣ.",
            },
        )


# ─── Group lessons (2026-09-14 redesign) ───────────────────────────────────────

def group_validation_rows(db: Session, session: TutoringSession) -> List[SessionValidation]:
    """Every SessionValidation row sharing this session's group slot,
    including `session`'s own — `[get_or_create_validation(db, session)]`
    for an individual (non-group) session. One row per enrolled student,
    same convention as app.services.livekit_video.group_sessions."""
    from app.services.livekit_video import group_sessions, group_slot_id

    slot_id = group_slot_id(db, session)
    if not slot_id:
        return [get_or_create_validation(db, session)]
    return [get_or_create_validation(db, s) for s in group_sessions(db, slot_id)]


def group_validation_stats(
    db: Session, session: TutoringSession, settings: PlatformSettings,
) -> Dict[str, Any]:
    """Aggregate validation counts for session's group (or the trivial
    1-student case for an individual session) — the numbers the teacher's
    "X/N ont validé" counter and the group-confirm/group-report gates are
    both computed from.

    `deadline_passed` is computed directly from teacher_ended_at + the
    admin-configured window (every sibling shares the same
    teacher_ended_at — see app.routers.session_validation.end_session's
    fan-out) rather than trusting each row's own `status` already having
    individually flipped to "expired": that transition only happens when
    THAT specific row gets polled (each student's own device polls their
    own session_id) — the teacher's group view is anchored to just one
    row and must not depend on every OTHER student's device having
    happened to poll recently."""
    rows = group_validation_rows(db, session)
    total = len(rows)
    validated = sum(1 for r in rows if r.student_validated_at is not None)
    finalized = sum(1 for r in rows if r.status in ("approved", "rejected"))
    already_reported = any(r.dispute_reason_code == "student_validation_neglect" for r in rows)
    percent = round((validated / total) * 100) if total else 0
    threshold = settings.trust_group_validation_threshold_percent

    anchor = next((r for r in rows if r.teacher_ended_at is not None), None)
    anchor_ended_at = (
        anchor.teacher_ended_at.replace(tzinfo=timezone.utc)
        if anchor and anchor.teacher_ended_at.tzinfo is None else (anchor.teacher_ended_at if anchor else None)
    )
    deadline_at = (
        anchor_ended_at + timedelta(hours=settings.student_validation_window_hours)
        if anchor_ended_at else None
    )
    deadline_passed = bool(deadline_at and datetime.now(timezone.utc) > deadline_at)

    return {
        "is_group": total > 1,
        "total": total,
        "validated": validated,
        "percent": percent,
        "threshold_percent": threshold,
        "threshold_met": percent >= threshold,
        "already_finalized": finalized == total,
        "already_reported": already_reported,
        "deadline_at": deadline_at,
        "deadline_passed": deadline_passed,
        "rows": rows,
    }


def group_confirm_and_finalize(
    db: Session, session: TutoringSession, settings: PlatformSettings, *, actor_user_id: UUID,
) -> Dict[str, Any]:
    """The teacher's single group-confirm action, once enough of the group
    has validated. For each sibling still awaiting a decision:
      - a student who personally validated goes through the exact same
        evaluate_and_finalize the individual flow always has (their own
        trust score still applies in full — GPS/online-duration/clean-
        history can still route THEM to admin_review even though the group
        as a whole cleared the bar);
      - a student who never personally validated is approved and paid
        directly (bypassing the trust-score gate for THEM specifically) —
        the group's own validation rate is standing in for their missing
        student_validation signal, per the product decision that a
        cleared group threshold vouches for the whole class, not just
        whoever happened to click.
    Raises ValueError if the threshold isn't met yet — callers translate
    that into a 409.
    """
    stats = group_validation_stats(db, session, settings)
    if not stats["threshold_met"]:
        raise ValueError(
            f"{stats['validated']}/{stats['total']} ont validé "
            f"({stats['percent']}%) — seuil requis {stats['threshold_percent']}%."
        )

    now = datetime.now(timezone.utc)
    results = []
    for sv in stats["rows"]:
        if sv.status in ("approved", "rejected"):
            results.append({"session_id": str(sv.session_id), "status": sv.status})
            continue
        sibling_session = db.get(TutoringSession, sv.session_id)
        if sibling_session is None:
            continue
        if sv.teacher_confirmed_at is None:
            sv.teacher_confirmed_at = now
        if sv.student_validated_at is not None:
            # This student validated individually — full normal evaluation,
            # unaffected by the group mechanism.
            db.add(sv)
            log_audit(db, session_id=sv.session_id, booking_id=sv.booking_id,
                      actor_user_id=actor_user_id, actor_ip=None, action="group_confirmed_individually_validated")
            evaluate_and_finalize(db, sibling_session, sv, settings)
        else:
            # Never personally validated — the group threshold vouches for
            # them instead. Approved and paid directly; trust_score is
            # still computed and stored for the audit trail, just not used
            # to gate this decision.
            clean = teacher_has_clean_history(db, sv.teacher_id)
            score, breakdown = compute_trust_score(sv, sibling_session, settings, teacher_has_clean_history=clean)
            breakdown["group_threshold_override"] = True
            sv.trust_score = score
            sv.trust_score_breakdown = breakdown
            sv.status = "approved"
            sv.payment_eligible_at = now
            db.add(sv)
            db.flush()
            credit_session_payout(db, sibling_session, sv)
            log_audit(db, session_id=sv.session_id, booking_id=sv.booking_id,
                      actor_user_id=actor_user_id, actor_ip=None,
                      action="group_confirmed_threshold_override", metadata={"trust_score": score})
            _notify(
                db, sv.student_id,
                {
                    "fr": "✅ Séance confirmée par ton professeur",
                    "en": "✅ Lesson confirmed by your teacher",
                    "ar": "✅ تم تأكيد الحصة من طرف أستاذك",
                    "tm": "✅ Tiɣimit tettwasentem sɣur uselmad-ik/inem",
                },
                {
                    "fr": "Ton professeur a confirmé cette séance de groupe — elle est validée même si tu n'avais pas cliqué sur \"Valider\".",
                    "en": "Your teacher confirmed this group lesson — it's validated even though you hadn't clicked \"Validate\".",
                    "ar": "أكد أستاذك هذه الحصة الجماعية — تم التحقق منها حتى لو لم تنقر على \"تحقق\".",
                    "tm": "Aselmad-ik/inem yesentem tiɣimit-agi n ugraw — tettwasenteḍ ɣas akken ur tenniḍ ara ɣef \"Senteḍ\".",
                },
                {"session_id": str(sv.session_id)},
            )
        results.append({"session_id": str(sv.session_id), "status": sv.status})

    _notify(
        db, session.teacher_id,
        {
            "fr": "✅ Groupe confirmé",
            "en": "✅ Group confirmed",
            "ar": "✅ تم تأكيد المجموعة",
            "tm": "✅ Agraw yettwasentem",
        },
        {
            "fr": f"Séance de groupe confirmée ({stats['validated']}/{stats['total']} élèves avaient validé) — paiement traité.",
            "en": f"Group lesson confirmed ({stats['validated']}/{stats['total']} students had validated) — payment processed.",
            "ar": f"تم تأكيد الحصة الجماعية ({stats['validated']}/{stats['total']} تلاميذ تحققوا) — تمت معالجة الدفع.",
            "tm": f"Tiɣimit n ugraw tettwasentem ({stats['validated']}/{stats['total']} inelmaden sentḍen) — axelaṣ yettwaselken.",
        },
        {"session_id": str(session.id)},
    )
    return {"results": results, "stats": {k: v for k, v in stats.items() if k != "rows"}}


def file_group_report(
    db: Session, session: TutoringSession, settings: PlatformSettings, *, actor_user_id: UUID, actor_ip: Optional[str],
) -> Dict[str, Any]:
    """The teacher's group-level escalation once the validation window has
    lapsed without clearing the threshold. Reuses the existing per-session
    student_validation_neglect dispute (app.routers.session_validation's
    dispute_session / _RECOGNIZED_REASON_CODES) rather than a parallel
    mechanism — fanned out to every sibling still stuck at 'expired', so
    the existing admin review queue, approve/reject endpoints, and the
    resulting student strike (see app/routers/admin/session_validation.py's
    approve_validation) all just work unmodified. Raises ValueError if the
    group isn't actually eligible yet (still within the window, or the
    threshold was already met) — callers translate that into a 409.
    """
    stats = group_validation_stats(db, session, settings)
    if stats["threshold_met"]:
        raise ValueError("Le seuil de validation du groupe est déjà atteint — rien à signaler.")
    if not stats["deadline_passed"]:
        raise ValueError("Le délai de validation des élèves n'est pas encore écoulé.")
    if stats["already_reported"]:
        raise ValueError("Ce groupe a déjà été signalé — en attente de la décision d'un administrateur.")

    now = datetime.now(timezone.utc)
    reported = []
    for sv in stats["rows"]:
        if sv.status not in ("expired", "awaiting_student_validation"):
            continue  # already resolved another way (validated, approved, disputed, ...)
        if sv.status == "awaiting_student_validation":
            # This specific row hasn't been individually polled since its
            # deadline passed (see group_validation_stats' own note) —
            # transition it now, same as _check_expiry would have.
            log_audit(db, session_id=sv.session_id, booking_id=sv.booking_id, actor_user_id=None,
                      actor_ip=None, action="validation_expired")
        sv.status = "admin_review"
        sv.dispute_reason = (
            f"Séance de groupe : seuil de validation non atteint "
            f"({stats['validated']}/{stats['total']}, {stats['percent']}% < {stats['threshold_percent']}% requis)."
        )
        sv.dispute_reason_code = "student_validation_neglect"
        sv.dispute_created_at = sv.dispute_created_at or now
        sv.dispute_filed_by = sv.dispute_filed_by or actor_user_id
        sv.updated_at = now
        db.add(sv)
        log_audit(db, session_id=sv.session_id, booking_id=sv.booking_id, actor_user_id=actor_user_id,
                  actor_ip=actor_ip, action="group_report_filed",
                  metadata={"group_validated": stats["validated"], "group_total": stats["total"]})
        _notify(
            db, sv.student_id,
            {
                "fr": "⚠️ Ton professeur a signalé une séance de groupe non validée",
                "en": "⚠️ Your teacher reported an unvalidated group lesson",
                "ar": "⚠️ أبلغ أستاذك عن حصة جماعية لم يتم التحقق منها",
                "tm": "⚠️ Aselmad-ik/inem yemmeslay-d ɣef tiɣimit n ugraw ur nettwasenteḍ ara",
            },
            {
                "fr": "Ton professeur a signalé à l'administration que cette séance de groupe a bien eu lieu. Un administrateur va vérifier.",
                "en": "Your teacher reported to the administration that this group lesson took place. An administrator will review it.",
                "ar": "أبلغ أستاذك الإدارة أن هذه الحصة الجماعية جرت فعلاً. سيقوم مسؤول بالمراجعة.",
                "tm": "Aselmad-ik/inem yemmeslay-d i unedbal belli tiɣimit-agi n ugraw tedṛa. Anedbal ad iẓer.",
            },
            {"session_id": str(sv.session_id)},
        )
        reported.append(str(sv.session_id))

    if reported:
        from app.models.profile import UserRole
        admin_ids = db.exec(select(UserRole).where(UserRole.role == "admin")).all()
        for ar in admin_ids:
            _notify(
                db, ar.user_id,
                {
                    "fr": "⚠️ Séance de groupe non validée — décision requise",
                    "en": "⚠️ Unvalidated group lesson — decision required",
                    "ar": "⚠️ حصة جماعية غير محققة — القرار مطلوب",
                    "tm": "⚠️ Tiɣimit n ugraw ur nettwasenteḍ ara — asentel yettusra",
                },
                {
                    "fr": f"{len(reported)} élève(s) n'ont pas validé une séance de groupe malgré le délai écoulé — intervention requise.",
                    "en": f"{len(reported)} student(s) didn't validate a group lesson despite the deadline passing — intervention required.",
                    "ar": f"{len(reported)} تلميذ(ة) لم يتحقق من حصة جماعية رغم انتهاء المهلة — التدخل مطلوب.",
                    "tm": f"{len(reported)} inelmaden ur senteḍen ara tiɣimit n ugraw ɣas yezri lawan — asenced yettusra.",
                },
                {"session_id": str(session.id)},
            )
    return {"reported_session_ids": reported, "stats": {k: v for k, v in stats.items() if k != "rows"}}
