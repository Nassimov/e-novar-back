from __future__ import annotations

"""Competitive Arena — Replay/AI Analysis/Recommendations API (Phase 4).

Ownership is validated on every endpoint: a player only ever sees their own
AI analysis/recommendations/roadmap, and only match participants (or an
admin) can view a match's replay — "Players can only access: Their own
replays. Administrators: All replays."
"""

import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlmodel import Session, func, select

from app.dependencies import get_current_user, get_db
from app.models.competitive import (
    CompetitiveMatch,
    CompetitiveMatchAiAnalysis,
    CompetitiveMatchPlayerStats,
    CompetitiveMatchReplay,
    CompetitiveRecommendation,
)
from app.models.profile import Profile
from app.schemas.competitive import (
    AiAnalysisResponse,
    RecommendationResponse,
    ReplayResponse,
    RoadmapStepResponse,
)
from app.services.competitive import analysis_service, match_service, replay_service, replay_timeline_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Competitive"])


def _assert_participant_or_admin(db: Session, match_id: UUID, user_id: UUID) -> CompetitiveMatch:
    match = match_service.get_match_or_404(db, match_id)
    participants = match_service.get_participants(db, match_id)
    if user_id not in [p.user_id for p in participants]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Ce replay ne t'appartient pas.")
    return match


def _assert_can_view_replay(db: Session, match_id: UUID, user_id: UUID) -> CompetitiveMatch:
    """Phase 12 — relaxes _assert_participant_or_admin: a replay may also
    be viewed by a non-participant if its owner set visibility to 'club' or
    'public' (see replay_service.assert_can_view_replay)."""
    match = match_service.get_match_or_404(db, match_id)
    replay = replay_service.get_replay_envelope(db, match_id)
    replay_service.assert_can_view_replay(db, match, replay, user_id)
    return match


@router.get("/matches/{match_id}/replay/full")
def get_full_replay(
    match_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])

    from app.core.rate_limiter import check_rate_limit
    from app.models.admin import PlatformSettings
    settings_row = db.get(PlatformSettings, True) or PlatformSettings()
    if not settings_row.feature_replay_enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Les replays sont actuellement désactivés.")
    check_rate_limit(
        key=f"arena_rl:replay_request:{user_id}", limit=settings_row.rate_limit_replay_request_per_10s,
        message="Trop de requêtes de replay. Merci de patienter.",
    )

    match = _assert_can_view_replay(db, match_id, user_id)

    envelope = replay_service.get_replay_envelope(db, match_id)
    if envelope is None:
        response: Dict[str, Any] = {
            "status": "pending", "score_timeline": [], "duration_sec": None, "generated_at": None, "questions": [],
            "visibility": "private", "visibility_locked": False,
        }
    else:
        questions = replay_service.assemble_question_timeline(db, match_id) if envelope.status == "ready" else []
        response = {
            "status": envelope.status,
            "score_timeline": envelope.score_timeline,
            "duration_sec": envelope.duration_sec,
            "generated_at": envelope.generated_at.isoformat() if envelope.generated_at else None,
            "questions": questions,
            "visibility": envelope.visibility,
            "visibility_locked": envelope.visibility_locked,
        }

    # Phase 10 Part C — Battle Royale replay enrichment: additive-only
    # fields (leaderboard evolution / eliminations timeline / winner_id) on
    # top of the exact same generic envelope every other match_type gets.
    # Phase 12 extends the exact same pattern to club_battle/tournament.
    try:
        if match.match_type == "battle_royale":
            response.update(replay_service.build_battle_royale_replay_enrichment(db, match))
        elif match.match_type == "club_battle":
            response.update(replay_service.build_club_battle_replay_enrichment(db, match))
        elif match.match_type == "tournament":
            response.update(replay_service.build_tournament_replay_enrichment(db, match))
    except Exception:
        logger.exception("replay enrichment failed for match=%s match_type=%s", match_id, match.match_type)

    response["stats"] = replay_service.get_replay_stats(db, match_id)
    response["is_favorited"] = match_id in set(replay_service.list_favorite_match_ids(db, user_id))
    return response


@router.get("/matches/{match_id}/replay/timeline")
def get_replay_timeline(
    match_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    match = _assert_can_view_replay(db, match_id, user_id)
    return {"match_id": str(match_id), "events": replay_timeline_service.build_timeline(db, match)}


@router.post("/matches/{match_id}/replay/view")
def post_replay_view(
    match_id: UUID,
    watch_duration_sec: int = Query(default=0, ge=0),
    completed: bool = Query(default=False),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    _assert_can_view_replay(db, match_id, user_id)
    replay_service.record_view(db, match_id, user_id, watch_duration_sec=watch_duration_sec, completed=completed)
    return {"recorded": True}


@router.post("/matches/{match_id}/replay/favorite")
def post_toggle_favorite(
    match_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    _assert_can_view_replay(db, match_id, user_id)
    favorited = replay_service.toggle_favorite(db, match_id, user_id)
    return {"favorited": favorited}


class SetReplayVisibilityRequest(BaseModel):
    visibility: str


@router.patch("/matches/{match_id}/replay/visibility")
def patch_replay_visibility(
    match_id: UUID,
    payload: SetReplayVisibilityRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    match = match_service.get_match_or_404(db, match_id)
    replay = replay_service.set_replay_visibility(db, match, user_id, payload.visibility)
    return {"visibility": replay.visibility}


@router.get("/profile/favorite-replays")
def get_favorite_replays(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    match_ids = replay_service.list_favorite_match_ids(db, user_id)
    items = []
    for match_id in match_ids:
        match = db.get(CompetitiveMatch, match_id)
        if match is None:
            continue
        items.append({"match_id": str(match_id), "match_type": match.match_type, "completed_at": match.completed_at.isoformat() if match.completed_at else None})
    return {"items": items}


@router.get("/replays/search")
def get_replay_search(
    player_id: Optional[UUID] = Query(default=None),
    subject_id: Optional[UUID] = Query(default=None),
    difficulty: Optional[str] = Query(default=None),
    match_type: Optional[str] = Query(default=None),
    tournament_id: Optional[UUID] = Query(default=None),
    club_id: Optional[UUID] = Query(default=None),
    date_from: Optional[date] = Query(default=None),
    date_to: Optional[date] = Query(default=None),
    favorites_only: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=50),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Spec's 'Replay Search' — browses PUBLIC/club-visible replays
    platform-wide (distinct from /replay-history below, which is scoped to
    the caller's own matches only)."""
    user_id = UUID(current_user["id"])
    return replay_service.search_public_replays(
        db, user_id, player_id=player_id, subject_id=subject_id, difficulty=difficulty, match_type=match_type,
        tournament_id=tournament_id, club_id=club_id, date_from=date_from, date_to=date_to,
        favorites_only=favorites_only, page=page, size=size,
    )


@router.get("/profile/performance-graphs")
def get_performance_graphs(
    limit: int = Query(default=20, ge=1, le=100),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    return analysis_service.get_performance_graphs(db, user_id, limit=limit)


@router.get("/profile/trends")
def get_trends(
    limit: int = Query(default=20, ge=1, le=100),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    return analysis_service.analyze_long_term_trends(db, user_id, limit=limit)


@router.get("/matches/{match_id}/ai-analysis", response_model=AiAnalysisResponse)
def get_ai_analysis(
    match_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from app.models.admin import PlatformSettings
    if not (db.get(PlatformSettings, True) or PlatformSettings()).feature_ai_analysis_enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="L'analyse IA est actuellement désactivée.")

    user_id = UUID(current_user["id"])
    _assert_participant_or_admin(db, match_id, user_id)

    row = db.get(CompetitiveMatchAiAnalysis, (match_id, user_id))
    if row is None:
        return AiAnalysisResponse(match_id=match_id, status="pending")
    return AiAnalysisResponse(
        match_id=row.match_id, status=row.status, overall_summary=row.overall_summary,
        strengths=row.strengths or [], weaknesses=row.weaknesses or [], behavior_notes=row.behavior_notes,
        mastery_level=row.mastery_level, confidence=row.confidence,
        progress_comparison=row.progress_comparison or {}, generated_at=row.generated_at, grade=row.grade,
    )


@router.get("/recommendations", response_model=List[RecommendationResponse])
def list_active_recommendations(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    rows = db.exec(
        select(CompetitiveRecommendation)
        .where(CompetitiveRecommendation.student_id == user_id)
        .where(CompetitiveRecommendation.status == "active")
        .order_by(CompetitiveRecommendation.created_at.desc())
    ).all()
    return rows


@router.post("/recommendations/{recommendation_id}/dismiss", response_model=RecommendationResponse)
def dismiss_recommendation(
    recommendation_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    rec = db.get(CompetitiveRecommendation, recommendation_id)
    if rec is None or rec.student_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recommandation introuvable.")
    rec.status = "dismissed"
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


@router.get("/roadmap", response_model=List[RoadmapStepResponse])
def get_roadmap(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from app.models.competitive import CompetitiveLearningRoadmapStep

    user_id = UUID(current_user["id"])
    rows = db.exec(
        select(CompetitiveLearningRoadmapStep)
        .where(CompetitiveLearningRoadmapStep.student_id == user_id)
        .order_by(CompetitiveLearningRoadmapStep.position)
    ).all()
    return rows


@router.get("/replay-history")
def list_replay_history(
    subject_id: Optional[UUID] = Query(default=None),
    difficulty: Optional[str] = Query(default=None),
    school_level_id: Optional[UUID] = Query(default=None),
    opponent_id: Optional[UUID] = Query(default=None),
    date_from: Optional[date] = Query(default=None),
    date_to: Optional[date] = Query(default=None),
    result_filter: Optional[str] = Query(default=None, alias="result"),  # win|loss|draw
    page: int = Query(default=1, ge=1),
    size: int = Query(default=10, ge=1, le=50),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Searchable replay history — spec's Replay Search filters."""
    user_id = UUID(current_user["id"])

    matches = match_service.list_my_matches(db, user_id, status_filter="completed")
    filtered = []
    for m in matches:
        if subject_id and subject_id not in m.subject_ids:
            continue
        if difficulty and m.difficulty != difficulty:
            continue
        if school_level_id and m.school_level_id != school_level_id:
            continue
        if date_from and m.completed_at and m.completed_at.date() < date_from:
            continue
        if date_to and m.completed_at and m.completed_at.date() > date_to:
            continue

        participants = match_service.get_participants(db, m.id)
        if opponent_id and opponent_id not in [p.user_id for p in participants]:
            continue

        stats = db.get(CompetitiveMatchPlayerStats, (m.id, user_id))
        if result_filter and (stats is None or stats.result != result_filter):
            continue

        opponent = next((p for p in participants if p.user_id != user_id), None)
        opponent_profile = db.get(Profile, opponent.user_id) if opponent else None
        filtered.append({
            "match_id": str(m.id),
            "opponent_name": opponent_profile.full_name if opponent_profile else None,
            "difficulty": m.difficulty,
            "completed_at": m.completed_at.isoformat() if m.completed_at else None,
            "result": stats.result if stats else None,
            "score": stats.score if stats else None,
        })

    total = len(filtered)
    filtered.sort(key=lambda r: r["completed_at"] or "", reverse=True)
    paged = filtered[(page - 1) * size: page * size]
    return {"items": paged, "total": total, "page": page, "size": size}
