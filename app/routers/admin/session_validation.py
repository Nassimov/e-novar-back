"""Admin review queue for the session validation workflow — disputed,
expired, or below-threshold sessions land here for a human decision. See
app/services/session_validation.py for the trust-score engine.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from app.dependencies import get_admin_user, get_db
from app.models.booking import Booking, TutoringSession
from app.models.profile import Profile
from app.models.session_validation import SessionValidation
from app.models.teacher import TeacherProfile
from app.schemas.session_validation import AdminDecisionRequest, AdminReviewItem, TrustScoreSettings
from app.services.pricing import get_platform_settings
from app.services.session_validation import credit_session_payout, generate_token, log_audit
from app.models.admin import PlatformSettings

router = APIRouter(tags=["Admin — Session Validation"])


def _trust_settings_dict(s: PlatformSettings) -> dict:
    return {
        "trust_weight_student_validation": s.trust_weight_student_validation,
        "trust_weight_teacher_confirmation": s.trust_weight_teacher_confirmation,
        "trust_weight_session_completed": s.trust_weight_session_completed,
        "trust_weight_online_duration": s.trust_weight_online_duration,
        "trust_weight_gps_proximity": s.trust_weight_gps_proximity,
        "trust_weight_clean_history": s.trust_weight_clean_history,
        "trust_auto_approve_threshold": s.trust_auto_approve_threshold,
        "trust_manual_review_threshold": s.trust_manual_review_threshold,
        "token_visible_minutes_before": s.token_visible_minutes_before,
        "student_validation_window_hours": s.student_validation_window_hours,
        "teacher_confirmation_window_hours": s.teacher_confirmation_window_hours,
        "gps_proximity_threshold_meters": s.gps_proximity_threshold_meters,
    }

_REVIEW_STATUSES = ("disputed", "admin_review", "expired")


@router.get("/", response_model=List[AdminReviewItem])
def list_review_queue(
    status_filter: Optional[str] = Query(None, alias="status"),
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    query = select(SessionValidation)
    if status_filter:
        query = query.where(SessionValidation.status == status_filter)
    else:
        query = query.where(SessionValidation.status.in_(_REVIEW_STATUSES))
    query = query.order_by(SessionValidation.updated_at.desc())
    rows = db.exec(query).all()

    session_ids = [r.session_id for r in rows]
    sessions = db.exec(select(TutoringSession).where(TutoringSession.id.in_(session_ids))).all() if session_ids else []
    session_map = {s.id: s for s in sessions}

    booking_ids = [s.booking_id for s in sessions if s.booking_id]
    bookings = db.exec(select(Booking).where(Booking.id.in_(booking_ids))).all() if booking_ids else []
    booking_map = {b.id: b for b in bookings}

    profile_ids = list({r.student_id for r in rows} | {r.teacher_id for r in rows})
    profiles = db.exec(select(Profile).where(Profile.id.in_(profile_ids))).all() if profile_ids else []
    profile_map = {p.id: p for p in profiles}

    result: List[AdminReviewItem] = []
    for r in rows:
        s = session_map.get(r.session_id)
        b = booking_map.get(s.booking_id) if s and s.booking_id else None
        student = profile_map.get(r.student_id)
        teacher = profile_map.get(r.teacher_id)
        result.append(AdminReviewItem(
            id=r.id,
            session_id=r.session_id,
            booking_id=r.booking_id,
            student_name=(student.full_name if student else None) or "—",
            teacher_name=(teacher.full_name if teacher else None) or "—",
            status=r.status,
            trust_score=r.trust_score,
            trust_score_breakdown=r.trust_score_breakdown,
            dispute_reason=r.dispute_reason,
            dispute_comment=r.dispute_comment,
            dispute_attachments=r.dispute_attachments,
            teacher_ended_at=r.teacher_ended_at,
            student_validated_at=r.student_validated_at,
            teacher_confirmed_at=r.teacher_confirmed_at,
            scheduled_at=s.scheduled_at if s else None,
            amount=b.amount if b else None,
            currency=b.currency if b else "DZD",
            created_at=r.created_at,
        ))
    return result


def _get_sv(db: Session, validation_id: UUID) -> SessionValidation:
    sv = db.get(SessionValidation, validation_id)
    if sv is None:
        raise HTTPException(status_code=404, detail="Validation record not found")
    return sv


@router.post("/{validation_id}/approve")
def approve_validation(
    validation_id: UUID,
    body: AdminDecisionRequest,
    admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Admin override — approves a disputed/expired/low-trust session and
    credits the payout, exactly as the automatic path would have."""
    sv = _get_sv(db, validation_id)
    if sv.status in ("approved", "rejected"):
        raise HTTPException(status_code=409, detail=f"Already decided ('{sv.status}')")

    session = db.get(TutoringSession, sv.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    admin_id = None
    if admin.get("id"):
        try:
            admin_id = UUID(admin["id"])
        except ValueError:
            admin_id = None

    now = datetime.now(timezone.utc)
    sv.status = "approved"
    sv.admin_decision = "approved"
    sv.admin_reviewed_by = admin_id
    sv.admin_review_note = body.note
    sv.admin_reviewed_at = now
    sv.payment_eligible_at = now
    db.add(sv)
    db.flush()

    credit_session_payout(db, session, sv)
    log_audit(db, session_id=session.id, booking_id=session.booking_id, actor_user_id=admin_id,
              actor_ip=None, action="admin_approved", metadata={"note": body.note})
    db.commit()
    return {"status": sv.status}


@router.post("/{validation_id}/reject")
def reject_validation(
    validation_id: UUID,
    body: AdminDecisionRequest,
    admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Admin rejects — the session is invalidated (didn't count as
    delivered): the teacher doesn't get paid AND the student is refunded —
    the platform never keeps money for a lesson it just ruled didn't
    happen/doesn't qualify. Does not touch the coarse sessions.status (still
    whatever it was, e.g. 'scheduled')."""
    sv = _get_sv(db, validation_id)
    if sv.status in ("approved", "rejected"):
        raise HTTPException(status_code=409, detail=f"Already decided ('{sv.status}')")

    admin_id = None
    if admin.get("id"):
        try:
            admin_id = UUID(admin["id"])
        except ValueError:
            admin_id = None

    sv.status = "rejected"
    sv.admin_decision = "rejected"
    sv.admin_reviewed_by = admin_id
    sv.admin_review_note = body.note
    sv.admin_reviewed_at = datetime.now(timezone.utc)
    db.add(sv)
    db.commit()

    refund_result = {"refunded": False, "requires_manual_action": False}
    if sv.booking_id:
        from app.services.pricing import PACK_SIZES
        from app.services.refunds import refund_amount_for_booking

        booking = db.get(Booking, sv.booking_id)
        if booking is not None:
            refund_amount = round(booking.amount / PACK_SIZES.get(booking.formula, 1))
            if refund_amount > 0:
                refund_result = refund_amount_for_booking(
                    db, booking, refund_amount,
                    note=f"Séance rejetée après vérification administrative." + (f" Motif : {body.note}" if body.note else ""),
                )
            session_row = db.get(TutoringSession, sv.session_id)
            if session_row is not None:
                # Clawback: if the teacher was already auto-credited for
                # this session (see credit_session_payout — this can happen
                # before an admin gets to review a disputed session), the
                # student refund above would otherwise leave the platform
                # eating the full loss while the teacher keeps their credit.
                # Deduct the same amount that was credited, clamped at 0 —
                # if the teacher already withdrew past it, the wallet can't
                # go negative here, so this is logged for manual follow-up
                # instead of silently under-clawing-back.
                if sv.payment_credited_at is not None and session_row.teacher_payout_amount:
                    teacher_profile = db.get(TeacherProfile, sv.teacher_id)
                    if teacher_profile is not None:
                        clawback_amount = session_row.teacher_payout_amount
                        shortfall = max(0, clawback_amount - teacher_profile.wallet_balance_dzd)
                        teacher_profile.wallet_balance_dzd = max(0, teacher_profile.wallet_balance_dzd - clawback_amount)
                        db.add(teacher_profile)
                        if shortfall > 0:
                            log_audit(
                                db, session_id=sv.session_id, booking_id=sv.booking_id, actor_user_id=admin_id,
                                actor_ip=None, action="payout_clawback_shortfall",
                                metadata={"teacher_id": str(sv.teacher_id), "clawback_amount": clawback_amount, "shortfall": shortfall},
                            )
                session_row.refund_percentage = 100
                session_row.refund_amount = refund_amount
                session_row.teacher_payout_amount = 0
                db.add(session_row)
                db.commit()

    from app.services.session_validation import _notify
    _notify(db, sv.teacher_id, "❌ Séance rejetée",
            f"Votre séance a été rejetée après vérification administrative." + (f" Motif : {body.note}" if body.note else ""))
    _notify(db, sv.student_id, "Séance rejetée — remboursement",
            "Ta séance a été invalidée après vérification administrative. "
            + ("Tu as été remboursé·e." if refund_result["refunded"] else "Ton remboursement est en cours de traitement."))

    log_audit(db, session_id=sv.session_id, booking_id=sv.booking_id, actor_user_id=admin_id,
              actor_ip=None, action="admin_rejected", metadata={"note": body.note, "refunded": refund_result["refunded"]})
    return {"status": sv.status, "refunded": refund_result["refunded"], "refund_requires_manual_action": refund_result["requires_manual_action"]}


@router.post("/{validation_id}/regenerate-token")
def admin_regenerate_token(
    validation_id: UUID,
    admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Escape hatch for a stuck session (e.g. student lost their code) —
    admin forces a fresh token. Returned once, same as the student's own
    view-token endpoint."""
    sv = _get_sv(db, validation_id)
    session = db.get(TutoringSession, sv.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    admin_id = None
    if admin.get("id"):
        try:
            admin_id = UUID(admin["id"])
        except ValueError:
            admin_id = None

    plaintext = generate_token(db, sv, actor_user_id=admin_id, actor_ip=None)
    db.commit()
    return {"token": plaintext, "expires_at": sv.token_expires_at}


@router.get("/settings/trust-score", response_model=TrustScoreSettings)
def get_trust_score_settings(
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    return _trust_settings_dict(get_platform_settings(db))


@router.put("/settings/trust-score", response_model=TrustScoreSettings)
def update_trust_score_settings(
    body: TrustScoreSettings,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    settings = db.get(PlatformSettings, True)
    if settings is None:
        settings = PlatformSettings(id=True)
    for field, value in body.model_dump().items():
        setattr(settings, field, value)
    settings.updated_at = datetime.now(timezone.utc)
    db.add(settings)
    db.commit()
    db.refresh(settings)
    return _trust_settings_dict(settings)
