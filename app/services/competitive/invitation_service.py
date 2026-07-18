from __future__ import annotations

"""
Invitation Service — Competitive Arena Phase 1.

A challenge is sent by the match creator to another student, identified by
that student's existing `student_code` (already used platform-wide for
parent-child linking — reused here rather than building a new opponent
discovery/search feature, which is out of scope for Phase 1's "architecture
only" mandate).
"""

from datetime import datetime, timezone
from typing import List
from uuid import UUID

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.models.competitive import CompetitiveInvitation, CompetitiveMatch, CompetitiveMatchParticipant
from app.models.profile import Profile, StudentProfile
from app.services.competitive import match_service
from app.services.notification_engine import emit


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


def create_invitation(db: Session, match: CompetitiveMatch, *, inviter_id: UUID, opponent_code: str) -> CompetitiveInvitation:
    if match.creator_id != inviter_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Seul le créateur du match peut inviter.")
    if match.status != "draft":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce match a déjà une invitation en cours.")

    invitee = _find_student_by_code(db, opponent_code)
    if invitee.id == inviter_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Impossible de te défier toi-même.")

    invitation = CompetitiveInvitation(match_id=match.id, inviter_id=inviter_id, invitee_id=invitee.id)
    db.add(invitation)
    match_service.transition(match, "waiting_for_opponent")
    db.add(match)
    db.commit()
    db.refresh(invitation)

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
        match_service.transition(match, "scheduled")
    else:
        match_service.transition(match, "waiting_room")
    db.add(match)
    db.commit()
    db.refresh(match)

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
    return invitation


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
