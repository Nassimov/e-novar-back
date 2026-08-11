from __future__ import annotations

"""Competitive Arena — Tournament System (Single Elimination), Part A —
Player-facing API (Phase 9). Mounted at /api/competitive alongside every
other Phase 1-8 competitive router (see app/main.py). Read (list/detail/
bracket/roster) + registration/unregistration only — the progression
engine, tournament WebSocket, and remaining admin actions are Part B's
job (see app/services/competitive/tournament_service.py's module
docstring)."""

from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session

from app.dependencies import get_current_user, get_db
from app.schemas.competitive import (
    TournamentBracketResponse,
    TournamentOut,
    TournamentParticipantOut,
    TournamentRewardOut,
)
from app.services.competitive import tournament_service
from app.services.competitive.feature_flags_service import require_feature

router = APIRouter(tags=["Competitive"], dependencies=[Depends(require_feature("tournament"))])


def _tournament_to_out(db: Session, tournament) -> TournamentOut:
    from app.models.competitive import CompetitiveTournamentParticipant
    from app.models.profile import Profile
    from sqlmodel import func, select

    organizer = db.get(Profile, tournament.organizer_id) if tournament.organizer_id else None
    participant_count = db.exec(
        select(func.count()).select_from(CompetitiveTournamentParticipant)
        .where(CompetitiveTournamentParticipant.tournament_id == tournament.id)
        .where(CompetitiveTournamentParticipant.status == "registered")
    ).one()
    waiting_list_count = db.exec(
        select(func.count()).select_from(CompetitiveTournamentParticipant)
        .where(CompetitiveTournamentParticipant.tournament_id == tournament.id)
        .where(CompetitiveTournamentParticipant.status == "waiting_list")
    ).one()
    return TournamentOut(
        **tournament.model_dump(),
        organizer_name=organizer.full_name if organizer else None,
        participant_count=participant_count,
        waiting_list_count=waiting_list_count,
    )


@router.get("/tournaments", response_model=List[TournamentOut])
def list_tournaments(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    subject_id: Optional[UUID] = Query(default=None),
    school_level_id: Optional[UUID] = Query(default=None),
    difficulty: Optional[str] = Query(default=None),
    tournament_type: Optional[str] = Query(default=None),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Never shows 'draft' tournaments — see tournament_service.
    list_tournaments_public."""
    tournaments = tournament_service.list_tournaments_public(
        db, status_filter=status_filter, subject_id=subject_id, school_level_id=school_level_id,
        difficulty=difficulty, tournament_type=tournament_type,
    )
    return [_tournament_to_out(db, t) for t in tournaments]


@router.get("/tournaments/{tournament_id}", response_model=TournamentOut)
def get_tournament_detail(
    tournament_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tournament = tournament_service.get_tournament_or_404(db, tournament_id)
    return _tournament_to_out(db, tournament)


@router.post("/tournaments/{tournament_id}/register", response_model=TournamentParticipantOut)
def register_for_tournament(
    tournament_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    tournament = tournament_service.get_tournament_or_404(db, tournament_id)
    participant = tournament_service.register_for_tournament(db, tournament, user_id)
    from app.models.profile import Profile
    profile = db.get(Profile, user_id)
    return TournamentParticipantOut(
        id=participant.id, tournament_id=participant.tournament_id, user_id=participant.user_id,
        full_name=profile.full_name if profile else None, avatar_url=profile.avatar_url if profile else None,
        seed=participant.seed, status=participant.status, waiting_list_position=participant.waiting_list_position,
        registered_at=participant.registered_at, eliminated_at=participant.eliminated_at,
        final_round_reached=participant.final_round_reached,
    )


@router.delete("/tournaments/{tournament_id}/register")
def unregister_from_tournament(
    tournament_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    tournament = tournament_service.get_tournament_or_404(db, tournament_id)
    tournament_service.unregister_from_tournament(db, tournament, user_id)
    return {"status": "unregistered"}


@router.get("/tournaments/{tournament_id}/participants", response_model=List[TournamentParticipantOut])
def get_tournament_participants(
    tournament_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tournament_service.get_tournament_or_404(db, tournament_id)
    return tournament_service.list_participants(db, tournament_id)


@router.get("/tournaments/{tournament_id}/bracket", response_model=TournamentBracketResponse)
def get_tournament_bracket(
    tournament_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tournament = tournament_service.get_tournament_or_404(db, tournament_id)
    return tournament_service.get_bracket_tree(db, tournament)


# ─── Phase 9 — Tournament System (Single Elimination), Part B ──────────────
# Player-facing read endpoints Part A didn't build: public reward config,
# post-tournament history/standings, the current user's own cross-
# tournament history, and per-tournament statistics.

@router.get("/tournaments/{tournament_id}/rewards", response_model=List[TournamentRewardOut])
def get_tournament_rewards_public(
    tournament_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tournament_service.get_tournament_or_404(db, tournament_id)
    return tournament_service.list_rewards(db, tournament_id)


@router.get("/tournaments/{tournament_id}/history")
def get_tournament_history(
    tournament_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tournament = tournament_service.get_tournament_or_404(db, tournament_id)
    if tournament.status != "completed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="L'historique n'est disponible qu'une fois le tournoi terminé.")
    return tournament_service.get_tournament_history(db, tournament)


@router.get("/profile/tournament-history")
def get_my_tournament_history(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    return tournament_service.get_user_tournament_history(db, user_id)


@router.get("/tournaments/{tournament_id}/statistics")
def get_tournament_statistics(
    tournament_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tournament = tournament_service.get_tournament_or_404(db, tournament_id)
    return tournament_service.get_tournament_statistics(db, tournament)
