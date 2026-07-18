"""Session validation & trust-score workflow — student/teacher-facing endpoints.

Mounted at /api/sessions (sibling of app/routers/sessions.py) as
/api/sessions/{session_id}/validation/*. See app/services/session_validation.py
for the state machine and app/routers/admin/session_validation.py for the
admin review queue.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from app.dependencies import get_current_user, get_db
from app.models.booking import TutoringSession
from app.schemas.session_validation import (
    DisputeRequest,
    GpsSubmitRequest,
    SessionValidationStatus,
    TokenViewResponse,
    ValidateSessionRequest,
)
from app.services.pricing import get_platform_settings
from app.services.session_validation import (
    consume_token,
    evaluate_and_finalize,
    generate_token,
    get_or_create_validation,
    log_audit,
    token_window_open,
)
from app.services.storage import upload_file
from sqlmodel import Session

router = APIRouter(tags=["Session Validation"])


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _load(db: Session, session_id: UUID, current_user: Dict[str, Any]):
    session = db.get(TutoringSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    uid = UUID(current_user["id"]) if current_user.get("id") else None
    role = current_user.get("role", "student")
    is_student = uid == session.student_id
    is_teacher = uid == session.teacher_id
    if not is_student and not is_teacher and role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    sv = get_or_create_validation(db, session)
    return session, sv, is_student, is_teacher


def _check_expiry(db: Session, session: TutoringSession, sv, settings) -> None:
    if sv.status == "awaiting_student_validation" and sv.teacher_ended_at:
        deadline = sv.teacher_ended_at + timedelta(hours=settings.student_validation_window_hours)
        if datetime.now(timezone.utc) > deadline:
            sv.status = "expired"
            db.add(sv)
            log_audit(db, session_id=session.id, booking_id=session.booking_id, actor_user_id=None,
                      actor_ip=None, action="validation_expired")
            db.commit()
            db.refresh(sv)


def _to_status(session: TutoringSession, sv, settings) -> SessionValidationStatus:
    scheduled_end = session.scheduled_at + timedelta(minutes=session.duration_min or 90)
    token_visible_at = session.scheduled_at - timedelta(minutes=settings.token_visible_minutes_before)
    deadline = (
        sv.teacher_ended_at + timedelta(hours=settings.student_validation_window_hours)
        if sv.teacher_ended_at else None
    )
    return SessionValidationStatus(
        session_id=session.id,
        booking_id=session.booking_id,
        status=sv.status,
        can_teacher_end=(sv.status == "scheduled" and datetime.now(timezone.utc) >= scheduled_end),
        can_view_token=token_window_open(sv, session, settings) and sv.status in ("scheduled", "awaiting_student_validation"),
        token_visible_at=token_visible_at,
        scheduled_end_at=scheduled_end,
        teacher_ended_at=sv.teacher_ended_at,
        student_validated_at=sv.student_validated_at,
        validation_method=sv.validation_method,
        teacher_confirmed_at=sv.teacher_confirmed_at,
        validation_deadline_at=deadline,
        dispute_reason=sv.dispute_reason,
        dispute_comment=sv.dispute_comment,
        trust_score=sv.trust_score,
        admin_decision=sv.admin_decision,
        admin_review_note=sv.admin_review_note,
        payment_credited_at=sv.payment_credited_at,
        gps_consent=sv.gps_consent,
    )


@router.get("/{session_id}/validation", response_model=SessionValidationStatus)
async def get_validation_status(
    session_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session, sv, _, _ = _load(db, session_id, current_user)
    settings = get_platform_settings(db)
    _check_expiry(db, session, sv, settings)
    return _to_status(session, sv, settings)


@router.post("/{session_id}/validation/end")
async def teacher_end_session(
    session_id: UUID,
    request: Request,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Teacher marks the lesson as over. This never credits payment directly —
    it only opens the student-validation window (spec: clicking this button
    must never trigger payout on its own)."""
    session, sv, _, is_teacher = _load(db, session_id, current_user)
    if not is_teacher and current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Only the session's teacher can end it")
    if sv.status != "scheduled":
        raise HTTPException(status_code=409, detail=f"Cannot end a session in status '{sv.status}'")

    scheduled_end = session.scheduled_at + timedelta(minutes=session.duration_min or 90)
    if datetime.now(timezone.utc) < scheduled_end:
        raise HTTPException(
            status_code=409,
            detail="La séance ne peut être terminée qu'après son heure de fin prévue.",
        )

    now = datetime.now(timezone.utc)
    sv.teacher_ended_at = now
    sv.status = "awaiting_student_validation"
    sv.updated_at = now
    db.add(sv)
    log_audit(db, session_id=session.id, booking_id=session.booking_id,
              actor_user_id=UUID(current_user["id"]) if current_user.get("id") else None,
              actor_ip=_client_ip(request), action="teacher_ended_session")

    from app.services.session_validation import _notify
    _notify(db, session.student_id, "📝 Séance terminée — validation requise",
            "Votre professeur a indiqué que la séance est terminée. Merci de la valider dans l'application.",
            {"session_id": str(session.id)})

    db.commit()
    return {"status": sv.status}


@router.get("/{session_id}/validation/token", response_model=TokenViewResponse)
async def view_token(
    session_id: UUID,
    request: Request,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Student (only) retrieves their session token. Only the plaintext
    returned by THIS call ever exists — the DB only ever stores its hash."""
    session, sv, is_student, _ = _load(db, session_id, current_user)
    if not is_student:
        raise HTTPException(status_code=403, detail="Only the student can view their session token")
    settings = get_platform_settings(db)
    if not token_window_open(sv, session, settings):
        raise HTTPException(
            status_code=409,
            detail=f"Le code ne sera visible que {settings.token_visible_minutes_before} minutes avant la séance.",
        )
    if sv.status not in ("scheduled", "awaiting_student_validation"):
        raise HTTPException(status_code=409, detail=f"Aucun code disponible pour une séance en statut '{sv.status}'")

    plaintext = generate_token(
        db, sv,
        actor_user_id=UUID(current_user["id"]),
        actor_ip=_client_ip(request),
    )
    db.commit()
    return TokenViewResponse(token=plaintext, expires_at=sv.token_expires_at)


@router.post("/{session_id}/validation/validate")
async def validate_session(
    session_id: UUID,
    body: ValidateSessionRequest,
    request: Request,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Consume the session token — either the student's own client submitting
    its cached token silently ('auto_send'), or the teacher typing in the
    code the student dictated out loud ('manual_entry'). Both paths run the
    exact same verification; only who called it and how differs."""
    session, sv, is_student, is_teacher = _load(db, session_id, current_user)
    if not is_student and not is_teacher:
        raise HTTPException(status_code=403, detail="Access denied")
    settings = get_platform_settings(db)
    _check_expiry(db, session, sv, settings)
    if body.method not in ("auto_send", "manual_entry"):
        raise HTTPException(status_code=400, detail="Invalid validation method")
    if sv.status != "awaiting_student_validation":
        raise HTTPException(status_code=409, detail=f"Cannot validate a session in status '{sv.status}'")
    if sv.student_validated_at is not None:
        raise HTTPException(status_code=409, detail="Cette séance a déjà été validée")

    if not consume_token(db, sv, body.token):
        log_audit(db, session_id=session.id, booking_id=session.booking_id,
                  actor_user_id=UUID(current_user["id"]), actor_ip=_client_ip(request),
                  action="validation_token_rejected", metadata={"method": body.method})
        db.commit()
        raise HTTPException(status_code=400, detail="Code invalide ou expiré")

    now = datetime.now(timezone.utc)
    sv.student_validated_at = now
    sv.validation_method = body.method
    sv.status = "validated"
    sv.updated_at = now
    db.add(sv)

    log_audit(db, session_id=session.id, booking_id=session.booking_id,
              actor_user_id=UUID(current_user["id"]), actor_ip=_client_ip(request),
              action="student_validated", metadata={"method": body.method})

    from app.services.session_validation import _notify
    _notify(db, session.teacher_id, "✅ Séance validée par l'élève",
            "Merci de confirmer la séance pour finaliser le paiement.",
            {"session_id": str(session.id)})

    db.commit()
    return {"status": sv.status}


@router.post("/{session_id}/validation/confirm")
async def teacher_confirm_session(
    session_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mandatory secondary teacher confirmation, required once the student
    has validated. Teacher cannot alter duration/attendance here — this is a
    pure confirm-or-dispute-elsewhere action. Triggers trust-score evaluation
    and, if the score clears the auto-approve bar, immediate payment."""
    session, sv, _, is_teacher = _load(db, session_id, current_user)
    if not is_teacher and current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Only the session's teacher can confirm it")
    if sv.status != "validated":
        raise HTTPException(status_code=409, detail=f"Cannot confirm a session in status '{sv.status}'")
    if sv.teacher_confirmed_at is not None:
        raise HTTPException(status_code=409, detail="Déjà confirmée")

    sv.teacher_confirmed_at = datetime.now(timezone.utc)
    db.add(sv)
    log_audit(db, session_id=session.id, booking_id=session.booking_id,
              actor_user_id=UUID(current_user["id"]) if current_user.get("id") else None,
              actor_ip="", action="teacher_confirmed")

    settings = get_platform_settings(db)
    evaluate_and_finalize(db, session, sv, settings)

    db.commit()
    db.refresh(sv)
    return {"status": sv.status, "trust_score": sv.trust_score}


_AUTO_RESOLVABLE_REASON_CODES = ("student_absent", "teacher_absent")


@router.post("/{session_id}/validation/dispute")
async def dispute_session(
    session_id: UUID,
    request: Request,
    reason: str = Form(...),
    comment: str = Form(""),
    reason_code: str = Form(""),
    files: list[UploadFile] = File(default=[]),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Either party can flag a problem — this always forces mandatory admin
    review and blocks payment, regardless of trust score.

    reason_code ('student_absent' | 'teacher_absent', optional): the
    structured in-person absence report — see
    app/workers/booking_tasks.py's task_auto_resolve_disputes. If the OTHER
    party never counters within in_person_dispute_auto_resolve_hours, it's
    auto-resolved in the filer's favor (payout/refund + strike, same
    consequences as the automatic online detection). If the other party
    calls this same endpoint on the same session while it's already
    disputed, that's treated as a counter — contested claims always go to
    a human (admin_review), never auto-resolve.

    Only usable on a CONFIRMED (paid/accepted) booking — disputing
    attendance on a booking nobody ever confirmed would let either party
    manufacture a payout/refund/strike out of nothing (there's no real
    money or commitment behind it yet).
    """
    session, sv, is_student, is_teacher = _load(db, session_id, current_user)
    if not is_student and not is_teacher:
        raise HTTPException(status_code=403, detail="Access denied")
    if sv.status in ("approved", "rejected", "cancelled"):
        raise HTTPException(status_code=409, detail=f"Cannot dispute a session in status '{sv.status}'")

    normalized_reason_code = reason_code if reason_code in _AUTO_RESOLVABLE_REASON_CODES else None
    if normalized_reason_code:
        from app.models.booking import Booking
        booking = db.get(Booking, session.booking_id) if session.booking_id else None
        if booking is None or booking.status != "confirmed":
            raise HTTPException(
                status_code=409,
                detail="Cette réservation n'est pas confirmée — impossible de signaler une absence dessus.",
            )

    attachment_urls = []
    for f in files or []:
        raw = await f.read()
        if not raw:
            continue
        url = upload_file(raw, f.filename or "attachment", f.content_type, folder=f"session-disputes/{session_id}")
        attachment_urls.append(url)

    now = datetime.now(timezone.utc)
    uid = UUID(current_user["id"]) if current_user.get("id") else None

    is_counter = sv.status == "disputed" and sv.dispute_filed_by is not None and sv.dispute_filed_by != uid
    if is_counter:
        # Contested claim — always a human decision from here, no auto-resolve.
        sv.dispute_countered_by = uid
        sv.dispute_countered_reason = reason
        sv.dispute_countered_at = now
        sv.dispute_auto_resolve_at = None
        sv.status = "admin_review"
        sv.updated_at = now
        db.add(sv)
        action = "dispute_countered"
    else:
        sv.status = "disputed"
        sv.dispute_reason = reason
        sv.dispute_comment = comment or None
        sv.dispute_attachments = attachment_urls
        sv.dispute_created_at = sv.dispute_created_at or now
        sv.dispute_reason_code = normalized_reason_code
        sv.dispute_filed_by = sv.dispute_filed_by or uid
        sv.updated_at = now
        if normalized_reason_code:
            settings = get_platform_settings(db)
            sv.dispute_auto_resolve_at = now + timedelta(hours=settings.in_person_dispute_auto_resolve_hours)
        db.add(sv)
        action = "dispute_filed"

    log_audit(db, session_id=session.id, booking_id=session.booking_id, actor_user_id=uid,
              actor_ip=_client_ip(request), action=action, metadata={"reason": reason, "reason_code": normalized_reason_code})

    from app.models.profile import UserRole
    from app.services.session_validation import _notify
    from sqlmodel import select as _select
    admin_ids = db.exec(_select(UserRole).where(UserRole.role == "admin")).all()
    for ar in admin_ids:
        _notify(db, ar.user_id,
                "⚠️ Litige contesté — décision requise" if is_counter else "⚠️ Litige sur une séance",
                f"Un litige a été signalé sur une séance ({reason}) — intervention requise.",
                {"session_id": str(session.id)})

    # The accused party MUST be told directly, not just admins — otherwise a
    # structured absence report with an auto-resolve deadline could go
    # completely unnoticed by the one person who could counter it, and
    # silently resolve in the filer's favor by default. This is the fix for
    # exactly that gap.
    if is_counter:
        # Notify whoever originally filed, since their claim is now contested.
        original_filer = sv.dispute_filed_by
        if original_filer is not None:
            _notify(db, original_filer, "Ton signalement a été contesté",
                    "L'autre partie a contesté ton signalement — un administrateur va trancher.",
                    {"session_id": str(session.id)})
    elif normalized_reason_code:
        accused_id = session.teacher_id if uid == session.student_id else session.student_id
        deadline_hours = get_platform_settings(db).in_person_dispute_auto_resolve_hours
        _notify(db, accused_id, "⚠️ Une absence a été signalée sur ta séance",
                (
                    f"{'Ton élève' if accused_id == session.teacher_id else 'Ton professeur'} a signalé une absence. "
                    f"Si tu ne contestes pas sous {deadline_hours}h, ce signalement sera automatiquement validé."
                ),
                {"session_id": str(session.id)})

    db.commit()
    return {"status": sv.status}


@router.post("/{session_id}/validation/online-connect")
async def record_online_connect(
    session_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session, sv, is_student, is_teacher = _load(db, session_id, current_user)
    if not is_student and not is_teacher:
        raise HTTPException(status_code=403, detail="Access denied")
    if sv.online_connected_at is None:
        sv.online_connected_at = datetime.now(timezone.utc)
        db.add(sv)
        db.commit()
    return {"online_connected_at": sv.online_connected_at}


@router.post("/{session_id}/validation/online-disconnect")
async def record_online_disconnect(
    session_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session, sv, is_student, is_teacher = _load(db, session_id, current_user)
    if not is_student and not is_teacher:
        raise HTTPException(status_code=403, detail="Access denied")
    now = datetime.now(timezone.utc)
    sv.online_disconnected_at = now
    if sv.online_connected_at:
        sv.online_duration_min = max(0, round((now - sv.online_connected_at).total_seconds() / 60))
    db.add(sv)
    db.commit()
    return {"online_duration_min": sv.online_duration_min}


@router.post("/{session_id}/validation/gps")
async def submit_gps(
    session_id: UUID,
    body: GpsSubmitRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Strictly opt-in — calling this endpoint at all IS the consent (the
    frontend only calls it after an explicit user opt-in prompt). One
    start/end snapshot per participant, never continuous tracking."""
    session, sv, is_student, is_teacher = _load(db, session_id, current_user)
    if not is_student and not is_teacher:
        raise HTTPException(status_code=403, detail="Access denied")

    now = datetime.now(timezone.utc)
    sv.gps_consent = True
    if is_student:
        sv.gps_student_lat = body.lat
        sv.gps_student_lng = body.lng
        sv.gps_student_at = now
    else:
        sv.gps_teacher_lat = body.lat
        sv.gps_teacher_lng = body.lng
        sv.gps_teacher_at = now
    db.add(sv)
    db.commit()
    return {"recorded": True}
