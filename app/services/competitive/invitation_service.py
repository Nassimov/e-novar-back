from __future__ import annotations

"""
Invitation Service — Competitive Arena Phase 1 (core) + Phase 2 (anti-spam,
blocking, module suspension, full paginated history, audit log).

A challenge is sent by the match creator to another student, identified by
that student's existing `student_code` (already used platform-wide for
parent-child linking — reused here rather than building a new opponent
discovery/search feature).
"""

from datetime import datetime, timedelta, timezone
from typing import List, Tuple
from uuid import UUID

from fastapi import HTTPException, status
from sqlmodel import Session, func, select

from app.core.redis import get_redis_client
from app.models.admin import PlatformSettings
from app.models.competitive import (
    CompetitiveInvitation,
    CompetitiveMatch,
    CompetitiveMatchParticipant,
    CompetitiveStatistics,
)
from app.models.profile import Profile, StudentProfile
from app.services.competitive import blocking_service, event_log_service, match_service
from app.services.notification_engine import emit


def _get_settings(db: Session) -> PlatformSettings:

    return db.get(PlatformSettings, True) or PlatformSettings()


def _find_student_by_code(db: Session, code: str) -> Profile:
    student = db.exec(
        select(StudentProfile).where(StudentProfile.student_code == code)
    ).first()
    if student is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Code élève introuvable.")
    profile = db.get(Profile, student.user_id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Élève introuvable.")
    return profile


def _assert_not_suspended(db: Session, user_id: UUID) -> None:
    stats = db.get(CompetitiveStatistics, user_id)
    if stats and stats.suspended_until and stats.suspended_until > datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Accès à l'Arène Compétitive suspendu pour ce compte.",
        )


def _check_anti_spam(db: Session, inviter_id: UUID) -> None:
    settings_row = _get_settings(db)

    # Redis-backed checks (cooldown/daily count) fail OPEN — anti-spam is a
    # secondary protection, never allowed to block the whole invite flow if
    # Redis hiccups (same posture as app/routers/messages.py's _rate_limit).
    try:
        r = get_redis_client()
        cooldown_key = f"competitive:invite_cooldown:{inviter_id}"
        if r.exists(cooldown_key):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Merci de patienter avant d'envoyer un nouveau défi.",
            )

        day_key = f"competitive:invite_count:{inviter_id}:{datetime.now(timezone.utc).date().isoformat()}"
        sent_today = int(r.get(day_key) or 0)
        if sent_today >= settings_row.competitive_max_invitations_per_day:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Nombre maximal de défis envoyés aujourd'hui atteint.",
            )

    except HTTPException:
        raise
    except Exception:
        pass

    pending_count = db.exec(
        select(func.count()).select_from(CompetitiveInvitation)
        .where(CompetitiveInvitation.inviter_id == inviter_id)
        .where(CompetitiveInvitation.status == "pending")
    ).one()
    if pending_count >= settings_row.competitive_max_pending_invitations:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Trop d'invitations en attente — attends une réponse avant d'en envoyer d'autres.",
        )


def _register_sent(db: Session, inviter_id: UUID) -> None:
    try:
        settings_row = _get_settings(db)
        r = get_redis_client()
        r.setex(f"competitive:invite_cooldown:{inviter_id}", settings_row.competitive_invitation_cooldown_seconds, "1")
        day_key = f"competitive:invite_count:{inviter_id}:{datetime.now(timezone.utc).date().isoformat()}"
        n = r.incr(day_key)
        if n == 1:
            r.expire(day_key, 86400)
    except Exception:
        pass


def create_invitation(db: Session, match: CompetitiveMatch, *, inviter_id: UUID, opponent_code: str) -> CompetitiveInvitation:
    if match.creator_id != inviter_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Seul le créateur du match peut inviter.")
    if match.status != "draft":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce match a déjà une invitation en cours.")

    _assert_not_suspended(db, inviter_id)
    _check_anti_spam(db, inviter_id)

    invitee = _find_student_by_code(db, opponent_code)
    if invitee.id == inviter_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Impossible de te défier toi-même.")
    if blocking_service.is_blocked_either_way(db, user_a=inviter_id, user_b=invitee.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Impossible de défier cet élève.")
    _assert_not_suspended(db, invitee.id)

    settings_row = _get_settings(db)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings_row.competitive_invitation_expiry_minutes)

    invitation = CompetitiveInvitation(
        match_id=match.id, inviter_id=inviter_id, invitee_id=invitee.id, expires_at=expires_at,
    )
    db.add(invitation)
    match_service.transition(match, "waiting_for_opponent")
    db.add(match)
    db.commit()
    db.refresh(invitation)
    _register_sent(db, inviter_id)

    event_log_service.log_event(
        db, match_id=match.id, actor_id=inviter_id, event_type="invitation_created",
        meta={"invitation_id": str(invitation.id), "invitee_id": str(invitee.id)},
    )

    inviter = db.get(Profile, inviter_id)
    emit(
        db,
        event_type="competitive_invitation_received",
        user_id=invitee.id,
        context={
            "inviter_name": inviter.full_name if inviter else "Un élève",
            "match_type": match.match_type,
        },
        data={"match_id": str(match.id), "invitation_id": str(invitation.id)},
        dedup_key=f"competitive_invitation:{invitation.id}",
    )
    return invitation


def get_invitation_or_404(db: Session, invitation_id: UUID) -> CompetitiveInvitation:
    invitation = db.get(CompetitiveInvitation, invitation_id)
    if invitation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation introuvable.")
    return invitation


def accept_invitation(db: Session, invitation: CompetitiveInvitation, *, user_id: UUID) -> CompetitiveMatch:
    if invitation.invitee_id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cette invitation ne t'est pas destinée.")
    if invitation.status != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cette invitation n'est plus valide.")
    if invitation.expires_at <= datetime.now(timezone.utc):
        invitation.status = "expired"
        db.add(invitation)
        db.commit()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cette invitation a expiré.")
    _assert_not_suspended(db, user_id)

    match = match_service.get_match_or_404(db, invitation.match_id)
    invitation.status = "accepted"
    invitation.responded_at = datetime.now(timezone.utc)
    db.add(invitation)

    existing = db.exec(
        select(CompetitiveMatchParticipant)
        .where(CompetitiveMatchParticipant.match_id == match.id)
        .where(CompetitiveMatchParticipant.user_id == user_id)
    ).first()
    if existing is None:
        db.add(CompetitiveMatchParticipant(match_id=match.id, user_id=user_id))

    match_service.transition(match, "accepted")
    if match.scheduled_at is not None:
        # Already had a known date/time at creation — nothing left to
        # negotiate, this IS the "Scheduled Match" branch.
        match_service.transition(match, "scheduled")
    # Else: stays 'accepted' — the frontend now presents the Immediate vs
    # Scheduled choice (see match_service.start_immediately() and
    # scheduling_service.propose_slot()).
    db.add(match)
    db.commit()
    db.refresh(match)

    event_log_service.log_event(
        db, match_id=match.id, actor_id=user_id, event_type="invitation_accepted",
        meta={"invitation_id": str(invitation.id)},
    )

    inviter = db.get(Profile, invitation.inviter_id)
    invitee = db.get(Profile, invitation.invitee_id)
    emit(
        db,
        event_type="competitive_invitation_accepted",
        user_id=invitation.inviter_id,
        context={"invitee_name": invitee.full_name if invitee else "Ton adversaire"},
        data={"match_id": str(match.id)},
        dedup_key=f"competitive_invitation_accepted:{invitation.id}",
    )
    return match


def decline_invitation(db: Session, invitation: CompetitiveInvitation, *, user_id: UUID) -> CompetitiveInvitation:
    if invitation.invitee_id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cette invitation ne t'est pas destinée.")
    if invitation.status != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cette invitation n'est plus valide.")

    invitation.status = "declined"
    invitation.responded_at = datetime.now(timezone.utc)
    db.add(invitation)

    match = match_service.get_match_or_404(db, invitation.match_id)
    if match.status == "waiting_for_opponent":
        match_service.transition(match, "draft")
        db.add(match)
    db.commit()
    db.refresh(invitation)

    event_log_service.log_event(
        db, match_id=match.id, actor_id=user_id, event_type="invitation_declined",
        meta={"invitation_id": str(invitation.id)},
    )

    invitee = db.get(Profile, invitation.invitee_id)
    emit(
        db,
        event_type="competitive_invitation_declined",
        user_id=invitation.inviter_id,
        context={"invitee_name": invitee.full_name if invitee else "Ton adversaire"},
        data={"match_id": str(invitation.match_id)},
        dedup_key=f"competitive_invitation_declined:{invitation.id}",
    )
    return invitation


def cancel_invitation(db: Session, invitation: CompetitiveInvitation, *, user_id: UUID) -> CompetitiveInvitation:
    if invitation.inviter_id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Seul l'auteur de l'invitation peut l'annuler.")
    if invitation.status != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cette invitation n'est plus valide.")

    invitation.status = "cancelled"
    invitation.responded_at = datetime.now(timezone.utc)
    db.add(invitation)

    match = match_service.get_match_or_404(db, invitation.match_id)
    if match.status == "waiting_for_opponent":
        match_service.transition(match, "draft")
        db.add(match)
    db.commit()
    db.refresh(invitation)

    event_log_service.log_event(
        db, match_id=match.id, actor_id=user_id, event_type="invitation_cancelled",
        meta={"invitation_id": str(invitation.id)},
    )

    emit(
        db, event_type="competitive_invitation_cancelled", user_id=invitation.invitee_id,
        data={"match_id": str(invitation.match_id)},
        dedup_key=f"competitive_invitation_cancelled:{invitation.id}",
    )
    return invitation


def list_history(
    db: Session, user_id: UUID, *, status_filter: str | None = None, page: int = 1, size: int = 20,
) -> Tuple[List[CompetitiveInvitation], int]:
    """Full paginated inbox (received + sent), newest first, any status."""
    base = select(CompetitiveInvitation).where(
        (CompetitiveInvitation.invitee_id == user_id) | (CompetitiveInvitation.inviter_id == user_id)
    )
    if status_filter:
        base = base.where(CompetitiveInvitation.status == status_filter)

    total = db.exec(select(func.count()).select_from(base.subquery())).one()
    items = list(
        db.exec(
            base.order_by(CompetitiveInvitation.created_at.desc())
            .offset((page - 1) * size)
            .limit(size)
        ).all()
    )
    return items, total


def list_received(db: Session, user_id: UUID) -> List[CompetitiveInvitation]:
    return list(
        db.exec(
            select(CompetitiveInvitation)
            .where(CompetitiveInvitation.invitee_id == user_id)
            .where(CompetitiveInvitation.status == "pending")
            .order_by(CompetitiveInvitation.created_at.desc())
        ).all()
    )


def list_sent(db: Session, user_id: UUID) -> List[CompetitiveInvitation]:
    return list(
        db.exec(
            select(CompetitiveInvitation)
            .where(CompetitiveInvitation.inviter_id == user_id)
            .where(CompetitiveInvitation.status == "pending")
            .order_by(CompetitiveInvitation.created_at.desc())
        ).all()
    )
