from __future__ import annotations

"""
Scheduler Service — Competitive Arena Phase 2.

Handles the "Scheduled Match" branch of Accept: unlimited proposal/counter-
proposal rounds until one slot is accepted, with calendar validation (no
past dates, admin-configured max scheduling window) and conflict detection
against both the student's booked tutoring sessions and their other active
competitive matches.
"""

from datetime import datetime, timedelta, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.models.admin import PlatformSettings
from app.models.booking import TutoringSession
from app.models.competitive import CompetitiveMatch, CompetitiveMatchParticipant, CompetitiveScheduleProposal
from app.services.competitive import match_service, question_engine
from app.services.notification_engine import emit


def _get_settings(db: Session) -> PlatformSettings:
    settings_row = db.get(PlatformSettings, True)
    return settings_row or PlatformSettings()


def _overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def check_scheduling_conflict(
    db: Session,
    *,
    user_ids: List[UUID],
    candidate_start: datetime,
    duration_minutes: int,
    exclude_match_id: Optional[UUID] = None,
) -> Optional[str]:
    candidate_end = candidate_start + timedelta(minutes=duration_minutes)

    booked = db.exec(
        select(TutoringSession)
        .where(TutoringSession.student_id.in_(user_ids))
        .where(TutoringSession.status.in_(["scheduled", "waiting", "live"]))
    ).all()
    for session in booked:
        session_end = session.scheduled_at + timedelta(minutes=session.duration_min or 60)
        if _overlaps(candidate_start, candidate_end, session.scheduled_at, session_end):
            return "Un des joueurs a déjà une séance réservée sur ce créneau."

    other_matches = db.exec(
        select(CompetitiveMatch)
        .join(CompetitiveMatchParticipant, CompetitiveMatchParticipant.match_id == CompetitiveMatch.id)
        .where(CompetitiveMatchParticipant.user_id.in_(user_ids))
        .where(CompetitiveMatch.status.in_(["scheduled", "waiting_room", "countdown", "in_progress"]))
        .where(CompetitiveMatch.scheduled_at.is_not(None))
    ).all()
    for other in other_matches:
        if exclude_match_id and other.id == exclude_match_id:
            continue
        other_duration = question_engine.estimate_match_duration_minutes(
            db, subject_ids=other.subject_ids, school_level_id=other.school_level_id,
            difficulty=other.difficulty, question_count=other.question_count,
        )
        other_end = other.scheduled_at + timedelta(minutes=other_duration)
        if _overlaps(candidate_start, candidate_end, other.scheduled_at, other_end):
            return "Un des joueurs a déjà un autre match compétitif prévu sur ce créneau."

    return None


def propose_slot(
    db: Session,
    match: CompetitiveMatch,
    *,
    proposed_by: UUID,
    proposed_at_time: datetime,
    timezone_label: Optional[str] = None,
) -> CompetitiveScheduleProposal:
    if match.status != "accepted":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="La planification n'est possible qu'après acceptation de l'invitation.",
        )
    participants = match_service.get_participants(db, match.id)
    participant_ids = [p.user_id for p in participants]
    if proposed_by not in participant_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tu ne participes pas à ce match.")

    now = datetime.now(timezone.utc)
    if proposed_at_time <= now:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="La date proposée est déjà passée.")

    settings_row = _get_settings(db)
    max_window = now + timedelta(days=settings_row.competitive_max_scheduling_days)
    if proposed_at_time > max_window:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"La date proposée dépasse la fenêtre de planification autorisée ({settings_row.competitive_max_scheduling_days} jours).",
        )

    duration = question_engine.estimate_match_duration_minutes(
        db, subject_ids=match.subject_ids, school_level_id=match.school_level_id,
        difficulty=match.difficulty, question_count=match.question_count,
    )
    conflict = check_scheduling_conflict(
        db, user_ids=participant_ids, candidate_start=proposed_at_time,
        duration_minutes=duration, exclude_match_id=match.id,
    )
    if conflict:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=conflict)

    # Any previous pending proposal on this match is superseded by this one.
    pending = db.exec(
        select(CompetitiveScheduleProposal)
        .where(CompetitiveScheduleProposal.match_id == match.id)
        .where(CompetitiveScheduleProposal.status == "pending")
    ).all()
    for p in pending:
        p.status = "superseded"
        db.add(p)

    proposal = CompetitiveScheduleProposal(
        match_id=match.id, proposed_by=proposed_by,
        proposed_at_time=proposed_at_time, timezone_label=timezone_label,
    )
    db.add(proposal)
    db.commit()
    db.refresh(proposal)

    other = next((uid for uid in participant_ids if uid != proposed_by), None)
    if other:
        from app.models.profile import Profile
        proposer = db.get(Profile, proposed_by)
        emit(
            db, event_type="competitive_schedule_proposed", user_id=other,
            context={"proposer_name": proposer.full_name if proposer else "Ton adversaire"},
            data={"match_id": str(match.id), "proposal_id": str(proposal.id)},
            dedup_key=f"competitive_schedule_proposed:{proposal.id}",
        )
    return proposal


def get_proposal_or_404(db: Session, proposal_id: UUID) -> CompetitiveScheduleProposal:
    proposal = db.get(CompetitiveScheduleProposal, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proposition introuvable.")
    return proposal


def accept_proposal(db: Session, proposal: CompetitiveScheduleProposal, *, user_id: UUID) -> CompetitiveMatch:
    if proposal.status != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cette proposition n'est plus valide.")
    if proposal.proposed_by == user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tu ne peux pas accepter ta propre proposition.")

    match = match_service.get_match_or_404(db, proposal.match_id)
    participants = match_service.get_participants(db, match.id)
    if user_id not in [p.user_id for p in participants]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tu ne participes pas à ce match.")

    proposal.status = "accepted"
    proposal.responded_at = datetime.now(timezone.utc)
    db.add(proposal)

    match.scheduled_at = proposal.proposed_at_time
    match_service.transition(match, "scheduled")
    db.add(match)
    db.commit()
    db.refresh(match)

    emit(
        db, event_type="competitive_schedule_accepted", user_id=proposal.proposed_by,
        context={"date": match.scheduled_at.isoformat()},
        data={"match_id": str(match.id)},
        dedup_key=f"competitive_schedule_accepted:{proposal.id}",
    )
    return match


def decline_proposal(db: Session, proposal: CompetitiveScheduleProposal, *, user_id: UUID) -> CompetitiveScheduleProposal:
    if proposal.status != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cette proposition n'est plus valide.")
    if proposal.proposed_by == user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tu ne peux pas refuser ta propre proposition.")

    participants = match_service.get_participants(db, proposal.match_id)
    if user_id not in [p.user_id for p in participants]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tu ne participes pas à ce match.")

    proposal.status = "declined"
    proposal.responded_at = datetime.now(timezone.utc)
    db.add(proposal)
    db.commit()
    db.refresh(proposal)

    from app.models.profile import Profile
    responder = db.get(Profile, user_id)
    emit(
        db, event_type="competitive_schedule_declined", user_id=proposal.proposed_by,
        context={"responder_name": responder.full_name if responder else "Ton adversaire"},
        data={"match_id": str(proposal.match_id)},
        dedup_key=f"competitive_schedule_declined:{proposal.id}",
    )
    return proposal


def list_proposals(db: Session, match_id: UUID) -> List[CompetitiveScheduleProposal]:
    return list(
        db.exec(
            select(CompetitiveScheduleProposal)
            .where(CompetitiveScheduleProposal.match_id == match_id)
            .order_by(CompetitiveScheduleProposal.created_at.desc())
        ).all()
    )
