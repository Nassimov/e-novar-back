from __future__ import annotations

"""Competitive Arena — Battle Royale, Part A — Player-facing API (Phase 10).
Mounted at /api/competitive alongside every other Phase 1-9 competitive
router (see app/main.py). Waiting room / join / leave / read-only detail
only — the live elimination/gameplay WebSocket and state endpoint are
Part B's job, exactly like Phase 9 Part A left the tournament progression
engine + WS to Part B (see app/services/competitive/battle_royale_service.py's
module docstring for the full handoff boundary)."""

from datetime import timedelta
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session

from app.dependencies import get_current_user, get_db
from app.schemas.competitive import (
    BattleRoyaleOut,
    BattleRoyaleParticipantOut,
    BattleRoyaleWaitingRoomResponse,
)
from app.services.competitive import battle_royale_service
from app.services.competitive.feature_flags_service import require_feature

router = APIRouter(tags=["Competitive"], dependencies=[Depends(require_feature("battle_royale"))])


def _battle_royale_to_out(db: Session, match, br) -> BattleRoyaleOut:
    countdown_deadline = None
    if br.current_phase == "countdown":
        countdown_deadline = br.updated_at + timedelta(seconds=br.auto_start_countdown_seconds)
    return BattleRoyaleOut(
        match_id=match.id, creator_id=match.creator_id, match_status=match.status,
        subject_ids=match.subject_ids, school_level_id=match.school_level_id, difficulty=match.difficulty,
        question_count=match.question_count, visibility=match.visibility,
        min_players=br.min_players, max_players=br.max_players,
        elimination_mode=br.elimination_mode, elimination_config=br.elimination_config,
        final_phase_threshold=br.final_phase_threshold, final_duel_method=br.final_duel_method,
        tie_break_rules=br.tie_break_rules, disconnect_policy=br.disconnect_policy,
        disconnect_grace_seconds=br.disconnect_grace_seconds, ranking_impact=br.ranking_impact,
        auto_start_countdown_seconds=br.auto_start_countdown_seconds, current_phase=br.current_phase,
        remaining_players_count=br.remaining_players_count, joined_count=br.remaining_players_count or 0,
        entry_cost_ep=br.entry_cost_ep, max_spectators=match.max_spectators,
        countdown_deadline=countdown_deadline, created_at=br.created_at, updated_at=br.updated_at,
    )


@router.get("/battle-royales", response_model=List[BattleRoyaleOut])
def list_battle_royales(
    subject_id: Optional[UUID] = Query(default=None),
    school_level_id: Optional[UUID] = Query(default=None),
    difficulty: Optional[str] = Query(default=None),
    max_players: Optional[int] = Query(default=None),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Public list — only non-draft/non-cancelled/non-expired, public-
    visibility matches (see battle_royale_service.list_battle_royales_public)."""
    pairs = battle_royale_service.list_battle_royales_public(
        db, subject_id=subject_id, school_level_id=school_level_id, difficulty=difficulty, max_players=max_players,
    )
    return [_battle_royale_to_out(db, m, br) for m, br in pairs]


@router.get("/battle-royales/{match_id}", response_model=BattleRoyaleOut)
def get_battle_royale_detail(
    match_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Config + counts only — NOT full live gameplay state (current
    question, scoreboard, etc.), which is Part B's WS/state endpoint's
    job."""
    match, br = battle_royale_service.get_battle_royale_or_404(db, match_id)
    return _battle_royale_to_out(db, match, br)


@router.post("/battle-royales/{match_id}/join", response_model=BattleRoyaleParticipantOut)
def join_battle_royale(
    match_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    match, br = battle_royale_service.get_battle_royale_or_404(db, match_id)
    participant = battle_royale_service.join_battle_royale(db, match, br, user_id)
    from app.models.profile import Profile
    profile = db.get(Profile, user_id)
    return BattleRoyaleParticipantOut(
        id=participant.id, match_id=participant.match_id, user_id=participant.user_id,
        full_name=profile.full_name if profile else None, avatar_url=profile.avatar_url if profile else None,
        is_ready=participant.is_ready, score=participant.score, lives=participant.lives,
        elimination_round=participant.elimination_round, final_rank=participant.final_rank,
        eliminated_at=participant.eliminated_at, joined_at=participant.joined_at, left_at=participant.left_at,
    )


@router.delete("/battle-royales/{match_id}/join")
def leave_battle_royale(
    match_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    match, br = battle_royale_service.get_battle_royale_or_404(db, match_id)
    battle_royale_service.leave_battle_royale(db, match, br, user_id)
    return {"status": "left"}


@router.get("/battle-royales/{match_id}/waiting-room", response_model=BattleRoyaleWaitingRoomResponse)
def get_battle_royale_waiting_room(
    match_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    match, br = battle_royale_service.get_battle_royale_or_404(db, match_id)
    payload = battle_royale_service.get_waiting_room(db, match, br)
    return BattleRoyaleWaitingRoomResponse(
        match_id=payload["match_id"], match_status=payload["match_status"], current_phase=payload["current_phase"],
        subject_ids=payload["subject_ids"], school_level_id=payload["school_level_id"], difficulty=payload["difficulty"],
        min_players=payload["min_players"], max_players=payload["max_players"], joined_count=payload["joined_count"],
        countdown_deadline=payload["countdown_deadline"], estimated_start_at=payload["estimated_start_at"],
        players=[BattleRoyaleParticipantOut(**p) for p in payload["players"]],
    )


# ─── Phase 10, Part C — player-facing read endpoints ───────────────────────

@router.get("/battle-royales/{match_id}/leaderboard")
def get_battle_royale_full_leaderboard(
    match_id: UUID,
    question_position: Optional[int] = Query(default=None),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Full standings at a given point in the match (or final standings if
    completed) — the complement to the live WS push's capped top-N."""
    match, _br = battle_royale_service.get_battle_royale_or_404(db, match_id)
    return {"match_id": match.id, "entries": battle_royale_service.get_full_leaderboard(db, match, question_position=question_position)}


@router.get("/profile/battle-royale-statistics")
def get_my_battle_royale_statistics(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    stats = battle_royale_service.get_or_create_battle_royale_statistics(db, user_id)
    matches_played = stats.matches_played
    return {
        "user_id": stats.user_id, "matches_played": matches_played, "wins": stats.wins,
        "top3_finishes": stats.top3_finishes, "top10_finishes": stats.top10_finishes,
        "best_rank": stats.best_rank, "longest_survival_position": stats.longest_survival_position,
        "avg_accuracy_pct": round(stats.total_accuracy_pct_sum / matches_played, 2) if matches_played else 0.0,
        "favorite_subject_id": stats.favorite_subject_id, "favorite_difficulty": stats.favorite_difficulty,
        "updated_at": stats.updated_at,
    }


@router.get("/profile/battle-royale-history")
def get_my_battle_royale_history(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    return battle_royale_service.get_user_battle_royale_history(db, user_id)
