from __future__ import annotations

"""
Match Service — Competitive Arena Phase 1.

Owns the match lifecycle state machine. A CompetitiveMatch row also
represents the lobby before it starts (see app/models/competitive.py). This
phase never drives a match past 'countdown' — 'in_progress' onward is
reserved for the future gameplay engine (phase 2+).
"""

from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.models.competitive import CompetitiveMatch, CompetitiveMatchParticipant

#: Legal status transitions. Terminal statuses (completed/cancelled/expired/
#: abandoned/disconnected) map to an empty set — nothing may leave them.
TRANSITIONS: dict[str, set[str]] = {
    # "waiting_room" was added as a DIRECT target from "draft" in Phase 10
    # (Battle Royale) — an open-lobby match type has no inviter/invitee, so
    # it never goes through "waiting_for_opponent"/"accepted"/"scheduled"
    # (see app/services/competitive/battle_royale_service.py's
    # create_battle_royale_match). "waiting_room" was already a legal
    # CompetitiveMatch.status value since Phase 1 — only the transition
    # GRAPH needed widening, not the status enum itself.
    "draft": {"waiting_for_opponent", "waiting_room", "cancelled", "expired"},
    "waiting_for_opponent": {"accepted", "draft", "cancelled", "expired"},
    "accepted": {"scheduled", "waiting_room", "cancelled"},
    "scheduled": {"waiting_room", "cancelled"},
    "waiting_room": {"countdown", "cancelled", "abandoned"},
    "countdown": {"in_progress", "waiting_room", "cancelled"},
    "in_progress": {"paused", "completed", "disconnected", "abandoned"},
    "paused": {"in_progress", "abandoned"},
    "completed": set(),
    "cancelled": set(),
    "expired": set(),
    "abandoned": set(),
    "disconnected": set(),
}


def transition(match: CompetitiveMatch, new_status: str) -> CompetitiveMatch:
    allowed = TRANSITIONS.get(match.status, set())
    if new_status not in allowed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Transition invalide : {match.status} -> {new_status}",
        )
    match.status = new_status
    match.updated_at = datetime.now(timezone.utc)
    return match


def create_match(
    db: Session,
    *,
    creator_id: UUID,
    match_type: str,
    subject_ids: List[UUID],
    school_level_id: Optional[UUID],
    difficulty: Optional[str],
    question_count: int,
    visibility: str,
    scheduled_at: Optional[datetime],
    is_ranked: bool = True,
) -> CompetitiveMatch:
    from app.services.competitive import ranking_service

    max_players = {"duel": 2, "club_battle": 10, "battle_royale": 20, "tournament": 8}.get(match_type, 2)
    # Phase 13 — a caller may ASK for ranked, but the admin-configured
    # allowed-modes list is always authoritative (never trust the frontend
    # for anything that moves Arena Rating); a caller may always downgrade
    # to casual (is_ranked=False) even for an allowed mode.
    resolved_is_ranked = is_ranked and ranking_service.is_match_type_ranked_eligible(db, match_type)
    match = CompetitiveMatch(
        match_type=match_type,
        creator_id=creator_id,
        created_by=creator_id,
        subject_ids=subject_ids,
        school_level_id=school_level_id,
        difficulty=difficulty,
        question_count=question_count,
        visibility=visibility,
        max_players=max_players,
        scheduled_at=scheduled_at,
        is_ranked=resolved_is_ranked,
    )
    db.add(match)
    db.commit()
    db.refresh(match)

    # The creator is always the first participant/lobby occupant.
    participant = CompetitiveMatchParticipant(match_id=match.id, user_id=creator_id)
    db.add(participant)
    db.commit()
    return match


def create_matched_pair(
    db: Session,
    *,
    player_a_id: UUID,
    player_b_id: UUID,
    subject_id: UUID,
    school_level_id: UUID,
    difficulty: str,
    question_count: int,
    is_ranked: bool = True,
) -> CompetitiveMatch:
    """Matchmaking-specific creation path (Phase 6). Unlike create_match's
    single-sided invitation flow (creator, then a separate invitee-accepts
    step), both players here have ALREADY mutually consented — both
    explicitly accepted the matchmaking proposal — so the match is created
    with BOTH participants at once and lands directly in 'accepted',
    skipping 'waiting_for_opponent' entirely."""
    from app.services.competitive import ranking_service

    resolved_is_ranked = is_ranked and ranking_service.is_match_type_ranked_eligible(db, "duel")
    match = CompetitiveMatch(
        match_type="duel",
        creator_id=player_a_id,
        created_by=player_a_id,
        subject_ids=[subject_id],
        school_level_id=school_level_id,
        difficulty=difficulty,
        question_count=question_count,
        visibility="private",
        max_players=2,
        is_ranked=resolved_is_ranked,
    )
    db.add(match)
    db.commit()
    db.refresh(match)

    db.add(CompetitiveMatchParticipant(match_id=match.id, user_id=player_a_id))
    db.add(CompetitiveMatchParticipant(match_id=match.id, user_id=player_b_id))
    transition(match, "waiting_for_opponent")
    transition(match, "accepted")
    db.add(match)
    db.commit()
    db.refresh(match)
    return match


def get_match_or_404(db: Session, match_id: UUID) -> CompetitiveMatch:
    match = db.get(CompetitiveMatch, match_id)
    if match is None or match.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match introuvable.")
    return match


def get_participants(db: Session, match_id: UUID) -> List[CompetitiveMatchParticipant]:
    return list(
        db.exec(
            select(CompetitiveMatchParticipant)
            .where(CompetitiveMatchParticipant.match_id == match_id)
            .where(CompetitiveMatchParticipant.left_at.is_(None))
        ).all()
    )


def get_participants_for_matches(db: Session, match_ids: List[UUID]) -> List[CompetitiveMatchParticipant]:
    """Batched variant of get_participants for a list of match ids (avoids N+1 in list endpoints)."""
    if not match_ids:
        return []
    return list(
        db.exec(
            select(CompetitiveMatchParticipant)
            .where(CompetitiveMatchParticipant.match_id.in_(match_ids))
            .where(CompetitiveMatchParticipant.left_at.is_(None))
        ).all()
    )


def assert_can_view(match: CompetitiveMatch, participants: List[CompetitiveMatchParticipant], user_id: UUID) -> None:
    if match.visibility == "public":
        return
    if match.creator_id == user_id or any(p.user_id == user_id for p in participants):
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Accès refusé à ce match.")


def list_my_matches(db: Session, user_id: UUID, status_filter: Optional[str] = None) -> List[CompetitiveMatch]:
    participant_match_ids = db.exec(
        select(CompetitiveMatchParticipant.match_id).where(CompetitiveMatchParticipant.user_id == user_id)
    ).all()
    if not participant_match_ids:
        return []
    query = select(CompetitiveMatch).where(CompetitiveMatch.id.in_(participant_match_ids)).where(
        CompetitiveMatch.deleted_at.is_(None)
    )
    if status_filter:
        query = query.where(CompetitiveMatch.status == status_filter)
    query = query.order_by(CompetitiveMatch.created_at.desc())
    return list(db.exec(query).all())


def cancel_match(db: Session, match: CompetitiveMatch, *, user_id: UUID, reason: Optional[str] = None) -> CompetitiveMatch:
    if match.creator_id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Seul le créateur peut annuler ce match.")
    transition(match, "cancelled")
    match.cancelled_at = datetime.now(timezone.utc)
    match.cancelled_reason = reason
    db.add(match)
    db.commit()
    db.refresh(match)
    return match


def enter_waiting_room(db: Session, match: CompetitiveMatch, *, user_id: UUID) -> CompetitiveMatch:
    """Either the 'Immediate Match' choice right after acceptance (from
    'accepted'), or manually joining once a 'Scheduled Match' 's slot is at
    hand (from 'scheduled') — Phase 2 has no automated countdown-triggered
    entry yet, a future phase can add that once the gameplay engine needs
    precise timing."""
    if match.status not in ("accepted", "scheduled"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ce match n'est pas prêt à passer en salle d'attente.",
        )
    participants = get_participants(db, match.id)
    if user_id not in [p.user_id for p in participants]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tu ne participes pas à ce match.")

    transition(match, "waiting_room")
    db.add(match)
    db.commit()
    db.refresh(match)
    return match


def set_ready(db: Session, match: CompetitiveMatch, *, user_id: UUID, ready: bool) -> CompetitiveMatch:
    if match.status not in ("waiting_room", "countdown"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Le lobby n'est pas encore ouvert pour ce match.",
        )
    participant = db.exec(
        select(CompetitiveMatchParticipant)
        .where(CompetitiveMatchParticipant.match_id == match.id)
        .where(CompetitiveMatchParticipant.user_id == user_id)
    ).first()
    if participant is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tu ne participes pas à ce match.")
    participant.is_ready = ready
    db.add(participant)
    db.commit()

    participants = get_participants(db, match.id)
    all_ready = len(participants) >= 2 and all(p.is_ready for p in participants)
    if all_ready and match.status == "waiting_room":
        transition(match, "countdown")
        db.add(match)
        db.commit()
        db.refresh(match)
    elif not all_ready and match.status == "countdown":
        # Someone un-readied — drop back out of countdown.
        transition(match, "waiting_room")
        db.add(match)
        db.commit()
        db.refresh(match)
    return match
