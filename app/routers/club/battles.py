from __future__ import annotations

"""Clash Club — Part C — Club Battles API (challenge handshake + read-only
battle state/history). Mounted at prefix="/api" (see app/main.py).

Ready-check/countdown/gameplay itself is NOT duplicated here — once a
challenge is accepted (a CompetitiveMatch is created), the player uses the
EXISTING generic `/matches/{id}/lobby/ready` and `/ws/competitive/{id}`
endpoints (app/routers/competitive/lobby.py, app/main.py) completely
unchanged, exactly like Battle Royale/Tournament matches already do."""

from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session

from app.dependencies import get_current_user, get_db
from app.models.competitive import CompetitiveMatch
from app.schemas.club import ChallengeOut, ClubBattleStateOut, CreateChallengeRequest, RespondChallengeRequest
from app.services.club import battle_service
from app.services.club.permission_service import require_permission
from app.services.competitive import match_service

router = APIRouter(tags=["Clubs"])


@router.post("/clubs/{club_id}/battles/challenges", response_model=ChallengeOut, status_code=201)
def create_challenge(
    club_id: UUID, payload: CreateChallengeRequest,
    current_user: Dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    return battle_service.create_challenge(
        db, challenger_club_id=club_id, created_by=user_id, opponent_club_id=payload.opponent_club_id,
        challenger_roster=payload.challenger_roster, team_size=payload.team_size, subject_ids=payload.subject_ids,
        school_level_id=payload.school_level_id, difficulty=payload.difficulty, question_count=payload.question_count,
        proposed_scheduled_at=payload.proposed_scheduled_at, message=payload.message,
    )


@router.get("/clubs/{club_id}/battles/challenges", response_model=List[ChallengeOut])
def list_challenges(
    club_id: UUID, direction: str = Query(default="incoming"), status_filter: Optional[str] = Query(default="pending", alias="status"),
    current_user: Dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db),
):
    require_permission(db, club_id, UUID(current_user["id"]), "view_analytics")
    return battle_service.list_challenges(db, club_id, direction=direction, status_filter=status_filter)


@router.post("/battles/challenges/{challenge_id}/respond")
def respond_to_challenge(
    challenge_id: UUID, payload: RespondChallengeRequest,
    current_user: Dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    challenge = battle_service.get_challenge_or_404(db, challenge_id)
    result = battle_service.respond_to_challenge(db, challenge, user_id, accept=payload.accept, opponent_roster=payload.opponent_roster)
    return {"status": result["challenge"].status, "match_id": result["match_id"]}


@router.delete("/battles/challenges/{challenge_id}", status_code=204)
def cancel_challenge(
    challenge_id: UUID, current_user: Dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    challenge = battle_service.get_challenge_or_404(db, challenge_id)
    battle_service.cancel_challenge(db, challenge, user_id)


@router.get("/club-battles/{match_id}", response_model=ClubBattleStateOut)
def get_club_battle(
    match_id: UUID, current_user: Dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db),
):
    match = match_service.get_match_or_404(db, match_id)
    return battle_service.get_club_battle_state(db, match)


@router.get("/clubs/{club_id}/battles")
def list_club_battles(
    club_id: UUID, upcoming_only: bool = Query(default=False), limit: int = Query(default=20, ge=1, le=100),
    current_user: Dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db),
):
    return battle_service.list_club_battles(db, club_id, upcoming_only=upcoming_only, limit=limit)
