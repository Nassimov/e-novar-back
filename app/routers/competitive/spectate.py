from __future__ import annotations

"""Competitive Arena — Spectator Mode, Live Reactions & Predictions API
(Phase 8). Mounted at /api/competitive alongside every other Phase 1-7
competitive router (see app/main.py). REST here covers everything that
isn't the real-time push itself (that's /ws/competitive/{match_id}/spectate,
see app/main.py + app/core/spectator_ws.py + gameplay_service.
compute_spectator_state)."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.competitive import CompetitiveLeague, CompetitiveMatch, CompetitiveStatistics
from app.models.profile import Profile
from app.schemas.competitive import (
    ChatHistoryResponse,
    ChatMessageCreateRequest,
    ChatMessageOut,
    ChatReportCreateRequest,
    LeagueOut,
    LiveMatchListResponse,
    LiveMatchOut,
    LiveMatchParticipantOut,
    PredictionCreateRequest,
    PredictionOut,
    PredictionSummaryResponse,
    ReactionCreateRequest,
    ReplayAvailabilityResponse,
    SpectatorStatsResponse,
)
from app.services.competitive import match_service, replay_service, spectator_presence_service, spectator_service
from app.services.competitive.feature_flags_service import require_feature

router = APIRouter(tags=["Competitive"], dependencies=[Depends(require_feature("spectator"))])


def _match_or_404_viewable(db: Session, match_id: UUID, user_id: UUID) -> CompetitiveMatch:
    match = match_service.get_match_or_404(db, match_id)
    participants = match_service.get_participants(db, match_id)
    match_service.assert_can_view(match, participants, user_id)
    return match


# ─── Live match discovery ───────────────────────────────────────────────────

def _duration_sec(match: CompetitiveMatch) -> Optional[int]:
    if not match.started_at:
        return None
    return int((datetime.now(timezone.utc) - match.started_at).total_seconds())


@router.get("/live-matches", response_model=LiveMatchListResponse)
def list_live_matches(
    subject_id: Optional[UUID] = Query(default=None),
    difficulty: Optional[str] = Query(default=None),
    league_id: Optional[UUID] = Query(default=None),
    language: Optional[str] = Query(default=None),
    match_type: Optional[str] = Query(default=None),
    sort: str = Query(default="newest"),  # most_spectators|highest_league|closest_match|newest|trending
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Only PUBLIC in_progress matches are discoverable here (private duels
    stay invite-only, matching competitive_matches' own visibility model —
    see match_service.assert_can_view). Duel-only today (Club Battle/Battle
    Royale/Tournament spectating isn't built yet — see migration 083's
    header), but nothing here assumes 2 players beyond the score_delta/
    "closest match" heuristic, which only ever applies to 2-player matches.
    """
    query = select(CompetitiveMatch).where(CompetitiveMatch.status == "in_progress").where(
        CompetitiveMatch.visibility == "public"
    )
    if match_type:
        query = query.where(CompetitiveMatch.match_type == match_type)
    matches = list(db.exec(query).all())

    league_by_user: Dict[UUID, Optional[CompetitiveLeague]] = {}

    def _league_for(user_id: UUID) -> Optional[CompetitiveLeague]:
        if user_id not in league_by_user:
            stats = db.get(CompetitiveStatistics, user_id)
            league_by_user[user_id] = db.get(CompetitiveLeague, stats.league_id) if stats and stats.league_id else None
        return league_by_user[user_id]

    rows: List[Dict[str, Any]] = []
    match_ids = [m.id for m in matches]
    spectator_counts = spectator_presence_service.get_counts(match_ids)

    for match in matches:
        if subject_id and subject_id not in match.subject_ids:
            continue
        if difficulty and match.difficulty != difficulty:
            continue

        participants = match_service.get_participants(db, match.id)
        if language and not any(
            (db.get(Profile, p.user_id) or Profile(id=p.user_id)).language == language for p in participants
        ):
            continue

        players = []
        for p in participants:
            profile = db.get(Profile, p.user_id)
            league = _league_for(p.user_id)
            players.append(LiveMatchParticipantOut(
                user_id=p.user_id, full_name=profile.full_name if profile else None,
                avatar_url=profile.avatar_url if profile else None,
                league=LeagueOut.model_validate(league) if league else None, score=p.score,
            ))
        # league_id filters to matches where at least one participant is
        # currently in that league — both players still show in the payload.
        if league_id and not any((p.league and p.league.id == league_id) for p in players):
            continue

        score_delta = abs(players[0].score - players[1].score) if len(players) == 2 else None
        spectator_count = spectator_counts.get(str(match.id), 0)

        rows.append({
            "match": match, "players": players, "score_delta": score_delta, "spectator_count": spectator_count,
        })

    if sort == "most_spectators":
        rows.sort(key=lambda r: r["spectator_count"], reverse=True)
    elif sort == "highest_league":
        rows.sort(key=lambda r: max((db.get(CompetitiveStatistics, p.user_id).rating if db.get(CompetitiveStatistics, p.user_id) else 0 for p in r["players"]), default=0), reverse=True)
    elif sort == "closest_match":
        rows.sort(key=lambda r: r["score_delta"] if r["score_delta"] is not None else 10**9)
    elif sort == "trending":
        # Simple composite: reward both a large live audience AND a close
        # score (closer = more exciting) — recent-spectator-GROWTH isn't
        # tracked as its own time series (would need a new table for a
        # "don't overthink it" ranking heuristic), so current spectator
        # count is used as a reasonable stand-in for "trending right now".
        # trending_score = spectator_count * 2 + max(0, 10 - score_delta)
        def _trending(r):
            delta = r["score_delta"] if r["score_delta"] is not None else 10
            return r["spectator_count"] * 2 + max(0, 10 - min(delta, 10))
        rows.sort(key=_trending, reverse=True)
    else:  # newest
        rows.sort(key=lambda r: r["match"].started_at or r["match"].created_at, reverse=True)

    total = len(rows)
    paged = rows[(page - 1) * size: page * size]

    items = []
    for r in paged:
        match = r["match"]
        position = match.current_question_position
        items.append(LiveMatchOut(
            match_id=match.id, match_type=match.match_type, subject_ids=match.subject_ids,
            school_level_id=match.school_level_id, difficulty=match.difficulty,
            question_count=match.question_count, current_position=position,
            started_at=match.started_at, duration_sec=_duration_sec(match),
            spectator_count=r["spectator_count"], players=r["players"], score_delta=r["score_delta"],
        ))

    return LiveMatchListResponse(items=items, total=total, page=page, size=size)


# ─── Predictions ────────────────────────────────────────────────────────────

@router.post("/matches/{match_id}/predictions", response_model=PredictionOut)
def create_prediction(
    match_id: UUID,
    payload: PredictionCreateRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    match = _match_or_404_viewable(db, match_id, user_id)
    return spectator_service.create_or_update_prediction(db, match, user_id=user_id, predicted_winner_id=payload.predicted_winner_id)


@router.get("/matches/{match_id}/predictions/summary", response_model=PredictionSummaryResponse)
def get_prediction_summary(
    match_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    match = _match_or_404_viewable(db, match_id, user_id)
    return spectator_service.get_prediction_summary(db, match, viewer_id=user_id)


# ─── Reactions ──────────────────────────────────────────────────────────────

@router.post("/matches/{match_id}/reactions", status_code=status.HTTP_202_ACCEPTED)
def create_reaction(
    match_id: UUID,
    payload: ReactionCreateRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    match = _match_or_404_viewable(db, match_id, user_id)
    spectator_service.create_reaction(db, match, user_id=user_id, emoji=payload.emoji)
    return {"status": "queued"}


# ─── Chat ───────────────────────────────────────────────────────────────────

@router.post("/matches/{match_id}/chat", response_model=ChatMessageOut, status_code=status.HTTP_201_CREATED)
def post_chat_message(
    match_id: UUID,
    payload: ChatMessageCreateRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    match = _match_or_404_viewable(db, match_id, user_id)
    message = spectator_service.create_chat_message(
        db, match, user_id=user_id, text=payload.text, reply_to_id=payload.reply_to_id,
    )
    profile = db.get(Profile, user_id)
    return ChatMessageOut(
        id=message.id, match_id=message.match_id, user_id=message.user_id,
        full_name=profile.full_name if profile else None, avatar_url=profile.avatar_url if profile else None,
        text=message.text, reply_to_id=message.reply_to_id, deleted_at=message.deleted_at, created_at=message.created_at,
    )


@router.delete("/matches/{match_id}/chat/{message_id}")
def delete_own_chat_message(
    match_id: UUID,
    message_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    spectator_service.delete_own_chat_message(db, match_id=match_id, message_id=message_id, user_id=user_id)
    return {"status": "deleted"}


@router.post("/matches/{match_id}/chat/{message_id}/report")
def report_chat_message(
    match_id: UUID,
    message_id: UUID,
    payload: ChatReportCreateRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    spectator_service.report_chat_message(db, message_id=message_id, reporter_id=user_id, reason=payload.reason)
    return {"status": "reported"}


@router.get("/matches/{match_id}/chat", response_model=ChatHistoryResponse)
def get_chat_history(
    match_id: UUID,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=30, ge=1, le=100),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    _match_or_404_viewable(db, match_id, user_id)
    items, total = spectator_service.list_chat_history(db, match_id, page=page, size=size)
    return ChatHistoryResponse(items=items, total=total, page=page, size=size)


# ─── Bookmarks ───────────────────────────────────────────────────────────────

@router.post("/matches/{match_id}/bookmark")
def bookmark_match(
    match_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    _match_or_404_viewable(db, match_id, user_id)
    spectator_service.add_bookmark(db, match_id=match_id, user_id=user_id)
    return {"status": "bookmarked"}


@router.delete("/matches/{match_id}/bookmark")
def unbookmark_match(
    match_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    spectator_service.remove_bookmark(db, match_id=match_id, user_id=user_id)
    return {"status": "unbookmarked"}


# ─── Spectator profile stats ────────────────────────────────────────────────

@router.get("/profile/spectator-stats", response_model=SpectatorStatsResponse)
def get_spectator_stats(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    return spectator_service.get_or_create_spectator_stats(db, user_id)


# ─── Replay availability (thin wrapper) ─────────────────────────────────────

@router.get("/matches/{match_id}/replay-availability", response_model=ReplayAvailabilityResponse)
def get_replay_availability(
    match_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    _match_or_404_viewable(db, match_id, user_id)
    envelope = replay_service.get_replay_envelope(db, match_id)
    status_value = envelope.status if envelope else "pending"
    return ReplayAvailabilityResponse(match_id=match_id, status=status_value, ready=status_value == "ready")
