"""Session validation & trust-score workflow — student/teacher-facing endpoints.

Mounted at /api/sessions (sibling of app/routers/sessions.py) as
/api/sessions/{session_id}/validation/*. See app/services/session_validation.py
for the state machine and app/routers/admin/session_validation.py for the
admin review queue.
"""
from __future__ import annotations

from datetime import datetime, timedelta
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
        if datetime.utcnow() > deadline:
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
        can_teacher_end=(sv.status == "scheduled" and datetime.utcnow() >= scheduled_end),
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
    if datetime.utcnow() < scheduled_end:
        raise HTTPException(
            status_code=409,
            detail="La séance ne peut être terminée qu'après son heure de fin prévue.",
        )

    now = datetime.utcnow()
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

    now = datetime.utcnow()
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

    sv.teacher_confirmed_at = datetime.utcnow()
    db.add(sv)
    log_audit(db, session_id=session.id, booking_id=session.booking_id,
              actor_user_id=UUID(current_user["id"]) if current_user.get("id") else None,
              actor_ip="", action="teacher_confirmed")

    settings = get_platform_settings(db)
    evaluate_and_finalize(db, session, sv, settings)

    db.commit()
    db.refresh(sv)
    return {"status": sv.status, "trust_score": sv.trust_score}


@router.post("/{session_id}/validation/dispute")
async def dispute_session(
    session_id: UUID,
    request: Request,
    reason: str = Form(...),
    comment: str = Form(""),
    files: list[UploadFile] = File(default=[]),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Either party can flag a problem — this always forces mandatory admin
    review and blocks payment, regardless of trust score."""
    session, sv, is_student, is_teacher = _load(db, session_id, current_user)
    if not is_student and not is_teacher:
        raise HTTPException(status_code=403, detail="Access denied")
    if sv.status in ("approved", "rejected", "cancelled"):
        raise HTTPException(status_code=409, detail=f"Cannot dispute a session in status '{sv.status}'")

    attachment_urls = []
    for f in files or []:
        raw = await f.read()
        if not raw:
            continue
        url = upload_file(raw, f.filename or "attachment", f.content_type, folder=f"session-disputes/{session_id}")
        attachment_urls.append(url)

    now = datetime.utcnow()
    sv.status = "disputed"
    sv.dispute_reason = reason
    sv.dispute_comment = comment or None
    sv.dispute_attachments = attachment_urls
    sv.dispute_created_at = now
    sv.updated_at = now
    db.add(sv)

    uid = UUID(current_user["id"]) if current_user.get("id") else None
    log_audit(db, session_id=session.id, booking_id=session.booking_id, actor_user_id=uid,
              actor_ip=_client_ip(request), action="dispute_filed", metadata={"reason": reason})

    from app.models.profile import UserRole
    from app.services.session_validation import _notify
    from sqlmodel import select as _select
    admin_ids = db.exec(_select(UserRole).where(UserRole.role == "admin")).all()
    for ar in admin_ids:
        _notify(db, ar.user_id, "⚠️ Litige sur une séance",
                f"Un litige a été signalé sur une séance ({reason}) — intervention requise.",
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
        sv.online_connected_at = datetime.utcnow()
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
    now = datetime.utcnow()
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

    now = datetime.utcnow()
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
