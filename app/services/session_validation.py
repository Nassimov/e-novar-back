"""Session validation & trust-score engine (migration 063 / spec: production
session-completion workflow). See app/routers/session_validation.py for the
HTTP surface.

Design summary
--------------
- A teacher can never single-handedly make themselves payment-eligible.
- Tokens are never stored in plaintext — only their SHA-256 hash. Viewing a
  token is idempotent-safe to repeat (regenerates + invalidates the previous
  one); *consuming* one is strictly single-use, enforced by an atomic
  conditional UPDATE so concurrent submissions can't double-consume it
  (classic replay/race protection — no application-level lock needed,
  Postgres's row-level locking on the UPDATE does the job).
- The trust score is a weighted sum of independent signals, all weights and
  thresholds pulled from PlatformSettings — never hardcoded (spec point 19).
- `token_method`/verification is written so a future QR code is just another
  way to *deliver* the same plaintext token to the verify function — the
  business logic never changes (spec point 14).
"""
from __future__ import annotations

import hashlib
import math
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple
from uuid import UUID

from sqlmodel import Session, select

from app.models.admin import PlatformSettings
from app.models.booking import Booking, TutoringSession
from app.models.profile import TeacherProfile
from app.models.session_validation import SessionValidation, SessionValidationAuditLog
from app.services.pricing import PACK_SIZES

# Unambiguous charset — no 0/O, 1/I/L, to avoid mis-hearing/mis-typing when
# the code is read aloud (the whole point of the manual-entry fallback).
_TOKEN_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


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


def _notify(db: Session, user_id: UUID, title: str, body: str, data: Optional[Dict[str, Any]] = None) -> None:
    from app.services.notification_engine import emit
    emit(
        db, event_type="session_validation", user_id=user_id,
        title_override=title, body_override=body, data=data or {},
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


# ─── Token lifecycle ──────────────────────────────────────────────────────────

def _hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def token_window_open(sv: SessionValidation, session: TutoringSession, settings: PlatformSettings) -> bool:
    """Token only becomes visible starting `token_visible_minutes_before`
    minutes before the session's scheduled start (spec point 2) — never
    before, regardless of what the client asks."""
    opens_at = session.scheduled_at - timedelta(minutes=settings.token_visible_minutes_before)
    return datetime.utcnow() >= opens_at


def generate_token(
    db: Session,
    sv: SessionValidation,
    *,
    actor_user_id: UUID,
    actor_ip: Optional[str],
) -> str:
    """Generate a fresh plaintext token, store only its hash, return the
    plaintext once. Safe to call again before consumption — each call
    invalidates whatever was generated before (old hash gets overwritten), so
    there is never more than one valid token per session at a time."""
    plaintext = f"{secrets.choice(_TOKEN_ALPHABET)}{''.join(secrets.choice(_TOKEN_ALPHABET) for _ in range(3))}-" \
                f"{''.join(secrets.choice(_TOKEN_ALPHABET) for _ in range(4))}"
    now = datetime.utcnow()
    sv.token_hash = _hash_token(plaintext)
    sv.token_expires_at = now + timedelta(hours=24)  # generous ceiling; the real gate is the validation window
    sv.token_shown_at = now
    sv.token_consumed_at = None
    sv.token_method = "app_token"
    sv.updated_at = now
    db.add(sv)
    log_audit(
        db, session_id=sv.session_id, booking_id=sv.booking_id,
        actor_user_id=actor_user_id, actor_ip=actor_ip, action="token_shown",
    )
    return plaintext


def consume_token(
    db: Session,
    sv: SessionValidation,
    plaintext: str,
) -> bool:
    """Atomically mark the current token consumed IF it matches and hasn't
    expired/been used yet. Returns False (never raises) on any mismatch, so
    callers can return a uniform "invalid or expired token" error without
    leaking which specific check failed (timing/oracle hardening).

    Concurrency: the UPDATE's WHERE clause includes `token_consumed_at IS
    NULL`, so if two requests race, only the first COMMIT wins — the second
    affects zero rows and this returns False. No explicit lock needed.
    """
    if not plaintext or sv.token_hash is None:
        return False
    candidate_hash = _hash_token(plaintext.strip().upper())
    now = datetime.utcnow()
    if sv.token_expires_at and now > sv.token_expires_at:
        return False

    from sqlalchemy import update as sa_update
    result = db.exec(
        sa_update(SessionValidation)
        .where(
            SessionValidation.id == sv.id,
            SessionValidation.token_hash == candidate_hash,
            SessionValidation.token_consumed_at.is_(None),
        )
        .values(token_consumed_at=now, updated_at=now)
    )
    consumed = result.rowcount > 0
    if consumed:
        db.refresh(sv)
    return consumed


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
    rather than double-crediting."""
    if sv.payment_credited_at is not None:
        return 0

    booking = db.get(Booking, session.booking_id) if session.booking_id else None
    if booking is None:
        return 0

    pack_size = PACK_SIZES.get(booking.formula, 1)
    payout = round(booking.amount / pack_size)

    session.status = "completed"
    if session.ended_at is None:
        session.ended_at = datetime.utcnow()
    session.teacher_payout_amount = payout
    db.add(session)

    teacher_profile = db.get(TeacherProfile, session.teacher_id)
    if teacher_profile is not None:
        teacher_profile.wallet_balance_dzd += payout
        db.add(teacher_profile)

    sv.payment_credited_at = datetime.utcnow()
    sv.updated_at = sv.payment_credited_at
    db.add(sv)

    log_audit(
        db, session_id=session.id, booking_id=session.booking_id,
        actor_user_id=None, actor_ip=None, action="payment_credited",
        metadata={"amount": payout},
    )
    _notify(
        db, session.teacher_id, "💰 Paiement crédité",
        f"{payout} DA ont été ajoutés à votre solde pour une séance validée.",
        {"session_id": str(session.id)},
    )
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
        sv.payment_eligible_at = datetime.utcnow()
        db.add(sv)
        db.flush()
        credit_session_payout(db, session, sv)
        _notify(db, sv.teacher_id, "✅ Séance approuvée", "Votre séance a été validée automatiquement — paiement crédité.")
        log_audit(db, session_id=session.id, booking_id=session.booking_id, actor_user_id=None,
                  actor_ip=None, action="auto_approved", metadata={"trust_score": score})
    else:
        sv.status = "admin_review"
        db.add(sv)
        log_audit(db, session_id=session.id, booking_id=session.booking_id, actor_user_id=None,
                  actor_ip=None, action="routed_to_admin_review", metadata={"trust_score": score})
        _notify(db, sv.teacher_id, "🕓 Séance en cours de vérification",
                "Votre séance nécessite une vérification supplémentaire avant le paiement.")
