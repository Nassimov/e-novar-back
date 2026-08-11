from __future__ import annotations

"""
Replay Service — Competitive Arena Phase 4 (reserved seam from Phase 1, now
implemented now that real gameplay data exists — see Phase 3).

The replay's actual content is never duplicated: it lives immutably in
competitive_match_questions/competitive_question_attempts (Phase 3). This
service only builds/stores the generation envelope
(competitive_match_replays: status + a precomputed score_timeline for fast
rendering) and assembles the full question-by-question view on read.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlmodel import Session, func, select

logger = logging.getLogger(__name__)

from app.models.competitive import (
    CompetitiveMatch,
    CompetitiveMatchQuestion,
    CompetitiveMatchReplay,
    CompetitiveQuestionAttempt,
    CompetitiveReplayFavorite,
    CompetitiveReplayView,
)
from app.models.practice import Question, QuestionChoice
from app.models.profile import Profile
from app.services.competitive import match_service

REPLAY_VISIBILITIES = ["private", "friends", "club", "public"]


def build_score_timeline(db: Session, match: CompetitiveMatch) -> List[Dict[str, Any]]:
    """One snapshot per revealed question: cumulative score per participant
    right after that question — "Visual Timeline... provide an intuitive
    visual evolution."""
    participants = match_service.get_participants(db, match.id)
    running: Dict[str, int] = {str(p.user_id): 0 for p in participants}

    mqs = db.exec(
        select(CompetitiveMatchQuestion)
        .where(CompetitiveMatchQuestion.match_id == match.id)
        .where(CompetitiveMatchQuestion.revealed_at.is_not(None))
        .order_by(CompetitiveMatchQuestion.position)
    ).all()

    timeline: List[Dict[str, Any]] = []
    for mq in mqs:
        attempts = db.exec(
            select(CompetitiveQuestionAttempt).where(CompetitiveQuestionAttempt.match_question_id == mq.id)
        ).all()
        for attempt in attempts:
            running[str(attempt.user_id)] = running.get(str(attempt.user_id), 0) + attempt.points_earned
        timeline.append({"position": mq.position, "scores": dict(running)})
    return timeline


def generate_replay(db: Session, match: CompetitiveMatch) -> CompetitiveMatchReplay:
    replay = db.get(CompetitiveMatchReplay, match.id)
    if replay is None:
        replay = CompetitiveMatchReplay(match_id=match.id)
    if replay.status == "ready":
        return replay  # immutable once generated — never regenerate
    replay.status = "processing"
    db.add(replay)
    db.commit()

    try:
        timeline = build_score_timeline(db, match)
        replay.score_timeline = timeline
        replay.duration_sec = (
            int((match.completed_at - match.started_at).total_seconds())
            if match.started_at and match.completed_at else None
        )
        replay.status = "ready"
        replay.generated_at = datetime.now(timezone.utc)
        db.add(replay)
        db.commit()

        from app.core import domain_events
        domain_events.publish(domain_events.REPLAY_GENERATED, match_id=match.id, match_type=match.match_type)
    except Exception:
        db.rollback()
        replay = db.get(CompetitiveMatchReplay, match.id) or CompetitiveMatchReplay(match_id=match.id)
        replay.status = "failed"
        replay.error = "generation_failed"
        db.add(replay)
        db.commit()

    db.refresh(replay)
    return replay


def get_replay_envelope(db: Session, match_id: UUID) -> CompetitiveMatchReplay | None:
    return db.get(CompetitiveMatchReplay, match_id)


def build_battle_royale_replay_enrichment(db: Session, match: CompetitiveMatch) -> Dict[str, Any]:
    """Phase 10 Part C — additive enrichment attached to the existing replay
    envelope (GET /matches/{match_id}/replay/full) ONLY for
    match.match_type == 'battle_royale'. The existing envelope mechanism
    (score_timeline/questions above) already works generically for ANY
    match_id — this never builds a parallel replay system, it only adds
    BR-specific fields the caller merges into the same dict:
      - leaderboard_evolution: CompetitiveBattleRoyaleRankingSnapshot rows,
        grouped by question_position (powers a "Leaderboard Evolution"
        timeline view).
      - eliminations_timeline: every 'battle_royale_eliminated' event
        logged for this match (event_log_service), ordered chronologically.
      - winner_id: the completed match's rank=1 participant, if any."""
    from app.models.competitive import CompetitiveBattleRoyaleRankingSnapshot
    from app.services.competitive import event_log_service, match_service

    snapshots = list(
        db.exec(
            select(CompetitiveBattleRoyaleRankingSnapshot)
            .where(CompetitiveBattleRoyaleRankingSnapshot.match_id == match.id)
            .order_by(CompetitiveBattleRoyaleRankingSnapshot.question_position.asc(), CompetitiveBattleRoyaleRankingSnapshot.rank.asc())
        ).all()
    )
    leaderboard_evolution: Dict[int, List[Dict[str, Any]]] = {}
    for s in snapshots:
        leaderboard_evolution.setdefault(s.question_position, []).append(
            {"user_id": str(s.user_id), "rank": s.rank, "score": s.score}
        )

    all_events = event_log_service.list_events(db, match.id)  # already ordered created_at DESC
    eliminations_timeline = [
        {
            "user_id": e.meta.get("user_id"), "final_rank": e.meta.get("final_rank"),
            "question_position": e.meta.get("question_position"), "at": e.created_at.isoformat(),
        }
        for e in reversed(all_events)  # chronological order
        if e.event_type == "battle_royale_eliminated"
    ]

    winner_id = None
    if match.status == "completed":
        participants = match_service.get_participants(db, match.id)
        winner = next((p for p in participants if p.final_rank == 1), None)
        winner_id = str(winner.user_id) if winner is not None else None

    return {
        "leaderboard_evolution": [
            {"question_position": pos, "standings": entries}
            for pos, entries in sorted(leaderboard_evolution.items())
        ],
        "eliminations_timeline": eliminations_timeline,
        "winner_id": winner_id,
    }


def assemble_question_timeline(db: Session, match_id: UUID) -> List[Dict[str, Any]]:
    """Full question-by-question review — safe to reveal in full since only
    ever called for a completed (or at least fully-revealed) match."""
    mqs = db.exec(
        select(CompetitiveMatchQuestion)
        .where(CompetitiveMatchQuestion.match_id == match_id)
        .where(CompetitiveMatchQuestion.revealed_at.is_not(None))
        .order_by(CompetitiveMatchQuestion.position)
    ).all()

    timeline = []
    for mq in mqs:
        question = db.get(Question, mq.question_id)
        choices = db.exec(select(QuestionChoice).where(QuestionChoice.question_id == mq.question_id)).all()
        attempts = db.exec(
            select(CompetitiveQuestionAttempt).where(CompetitiveQuestionAttempt.match_question_id == mq.id)
        ).all()
        answers = []
        for a in attempts:
            profile = db.get(Profile, a.user_id)
            answers.append({
                "user_id": str(a.user_id),
                "full_name": profile.full_name if profile else None,
                "selected_choice_ids": [str(c) for c in a.selected_choice_ids],
                "is_correct": a.is_correct,
                "points_earned": a.points_earned,
                "response_time_ms": a.response_time_ms,
                "mistake_category": a.mistake_category,
            })
        timeline.append({
            "position": mq.position,
            "revealed_at": mq.revealed_at.isoformat() if mq.revealed_at else None,
            "statement": question.statement if question else "",
            "explanation": question.explanation if question else "",
            "difficulty": question.difficulty if question else None,
            "choices": [{"id": str(c.id), "label": c.label, "text": c.text, "is_correct": c.is_correct} for c in choices],
            "answers": answers,
        })
    return timeline


# ─── Phase 12: Club Battle / Tournament replay enrichment ──────────────────
# Mirrors build_battle_royale_replay_enrichment's exact pattern (Phase 10) —
# additive-only fields merged onto the SAME generic envelope, never a
# parallel replay system.

def build_club_battle_replay_enrichment(db: Session, match: CompetitiveMatch) -> Dict[str, Any]:
    from app.models.club import Club, ClubBattleMatch

    cb = db.get(ClubBattleMatch, match.id)
    if cb is None:
        return {}
    club_a = db.get(Club, cb.club_a_id)
    club_b = db.get(Club, cb.club_b_id)
    participants = match_service.get_participants(db, match.id)
    return {
        "club_battle": {
            "team_size": cb.team_size,
            "club_a": {"club_id": str(cb.club_a_id), "name": club_a.name if club_a else None, "tag": club_a.tag if club_a else None, "score": cb.club_a_score},
            "club_b": {"club_id": str(cb.club_b_id), "name": club_b.name if club_b else None, "tag": club_b.tag if club_b else None, "score": cb.club_b_score},
            "winner_club_id": str(cb.winner_club_id) if cb.winner_club_id else None,
            "mvp_user_id": str(cb.mvp_user_id) if cb.mvp_user_id else None,
            "rosters": {
                "a": [str(p.user_id) for p in participants if p.team_side == "a"],
                "b": [str(p.user_id) for p in participants if p.team_side == "b"],
            },
        },
    }


def build_tournament_replay_enrichment(db: Session, match: CompetitiveMatch) -> Dict[str, Any]:
    from app.models.competitive import CompetitiveTournament, CompetitiveTournamentMatch, CompetitiveTournamentRound

    tm = db.exec(select(CompetitiveTournamentMatch).where(CompetitiveTournamentMatch.match_id == match.id)).first()
    if tm is None:
        return {}
    tournament = db.get(CompetitiveTournament, tm.tournament_id)
    round_row = db.get(CompetitiveTournamentRound, tm.round_id)
    return {
        "tournament": {
            "tournament_id": str(tm.tournament_id), "tournament_name": tournament.name if tournament else None,
            "round_number": round_row.round_number if round_row else None, "round_name": round_row.round_name if round_row else None,
            "bracket_position": tm.bracket_position, "winner_id": str(tm.winner_id) if tm.winner_id else None,
        },
    }


# ─── Phase 12: Privacy ──────────────────────────────────────────────────────

def assert_can_view_replay(db: Session, match: CompetitiveMatch, replay: Optional[CompetitiveMatchReplay], viewer_id: UUID) -> None:
    """Raises 403 on failure. Participants can always see their own
    match's replay regardless of visibility (visibility governs OTHER
    people's access, never the players' own) — mirrors club_service.
    search_clubs' "privacy governs join, not visibility" precedent, applied
    here to "privacy governs OUTSIDE access, not participant access"."""
    if replay is not None and replay.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ce replay a été supprimé.")

    participants = match_service.get_participants(db, match.id)
    if viewer_id in [p.user_id for p in participants]:
        return

    visibility = replay.visibility if replay is not None else "private"
    if visibility == "public":
        return
    if visibility == "club":
        from app.models.club import ClubMember
        from app.models.competitive import CompetitiveMatchParticipant
        participant_club_ids = set(db.exec(
            select(CompetitiveMatchParticipant.club_id).where(CompetitiveMatchParticipant.match_id == match.id).where(CompetitiveMatchParticipant.club_id.is_not(None))
        ).all())
        if participant_club_ids:
            viewer_club_ids = set(db.exec(
                select(ClubMember.club_id).where(ClubMember.user_id == viewer_id).where(ClubMember.status == "active")
            ).all())
            if participant_club_ids & viewer_club_ids:
                return
    # 'private' and 'friends' (no friends-graph exists — see migration 092's
    # header) both fall through to: only participants may view.
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Ce replay est privé.")


def get_or_create_replay_privacy(db: Session, match_id: UUID) -> CompetitiveMatchReplay:
    replay = db.get(CompetitiveMatchReplay, match_id)
    if replay is None:
        from app.models.admin import PlatformSettings
        settings_row = db.get(PlatformSettings, True) or PlatformSettings()
        replay = CompetitiveMatchReplay(match_id=match_id, visibility=settings_row.competitive_replay_default_visibility)
        db.add(replay)
        db.commit()
        db.refresh(replay)
    return replay


def set_replay_visibility(db: Session, match: CompetitiveMatch, actor_id: Optional[UUID], visibility: str, *, is_admin: bool = False) -> CompetitiveMatchReplay:
    if visibility not in REPLAY_VISIBILITIES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Visibilité invalide (options : {REPLAY_VISIBILITIES}).")
    replay = get_or_create_replay_privacy(db, match.id)
    if replay.visibility_locked and not is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="La visibilité de ce replay a été verrouillée par un administrateur.")
    if not is_admin:
        participants = match_service.get_participants(db, match.id)
        if actor_id not in [p.user_id for p in participants]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Seuls les participants peuvent modifier la visibilité de ce replay.")
    replay.visibility = visibility
    if is_admin:
        replay.visibility_locked = True
    db.add(replay)
    db.commit()
    db.refresh(replay)
    return replay


# ─── Phase 12: Favorites / views / analytics ────────────────────────────────

def toggle_favorite(db: Session, match_id: UUID, user_id: UUID) -> bool:
    """Returns the new favorited state (True/False)."""
    existing = db.exec(
        select(CompetitiveReplayFavorite).where(CompetitiveReplayFavorite.match_id == match_id).where(CompetitiveReplayFavorite.user_id == user_id)
    ).first()
    if existing is not None:
        db.delete(existing)
        db.commit()
        return False
    db.add(CompetitiveReplayFavorite(match_id=match_id, user_id=user_id))
    db.commit()
    return True


def list_favorite_match_ids(db: Session, user_id: UUID) -> List[UUID]:
    return list(db.exec(select(CompetitiveReplayFavorite.match_id).where(CompetitiveReplayFavorite.user_id == user_id)).all())


def record_view(db: Session, match_id: UUID, user_id: UUID, *, watch_duration_sec: int = 0, completed: bool = False) -> None:
    db.add(CompetitiveReplayView(match_id=match_id, user_id=user_id, watch_duration_sec=max(0, watch_duration_sec), completed=completed))
    db.commit()

    if completed:
        try:
            from app.services.competitive import mission_service
            mission_service.record_event(db, user_id=user_id, metric_key="arena_replay_watched", amount=1)
        except Exception:
            logger.debug("replay_service.record_view: mission record_event failed", exc_info=True)


def delete_replay(db: Session, match_id: UUID, actor_id: Optional[UUID] = None) -> None:
    """Admin-only (spec: 'Only administrators may override' privacy; the
    same reasoning applies to deletion — letting either duel participant
    unilaterally hide a replay the other wants to keep is a griefing vector
    a plain 'set visibility to private' doesn't have, since visibility
    already never hides a replay from its own participants)."""
    replay = get_or_create_replay_privacy(db, match_id)
    replay.deleted_at = datetime.now(timezone.utc)
    replay.deleted_by = actor_id
    db.add(replay)
    db.commit()


def restore_replay(db: Session, match_id: UUID) -> None:
    replay = get_or_create_replay_privacy(db, match_id)
    replay.deleted_at = None
    replay.deleted_by = None
    db.add(replay)
    db.commit()


def get_replay_stats(db: Session, match_id: UUID) -> Dict[str, Any]:
    views = list(db.exec(select(CompetitiveReplayView).where(CompetitiveReplayView.match_id == match_id)).all())
    view_count = len(views)
    completed_count = sum(1 for v in views if v.completed)
    favorite_count = db.exec(select(func.count()).select_from(
        select(CompetitiveReplayFavorite.id).where(CompetitiveReplayFavorite.match_id == match_id).subquery()
    )).one()
    avg_watch_time = round(sum(v.watch_duration_sec for v in views) / view_count) if view_count else 0
    completion_rate = round((completed_count / view_count) * 100, 1) if view_count else 0.0
    return {
        "view_count": view_count, "favorite_count": favorite_count or 0,
        "avg_watch_time_sec": avg_watch_time, "completion_rate_pct": completion_rate,
    }


# ─── Phase 12: Public Replay Search ──────────────────────────────────────────
# Distinct from the player-scoped /replay-history (own matches only,
# app/routers/competitive/analysis.py) — this searches every PUBLIC (or
# club-visible-to-the-viewer) replay platform-wide, spec's "Replay Search"
# section ("Search by Player, Subject, Difficulty, Season, League, Date,
# Tournament, Club").

def search_public_replays(
    db: Session, viewer_id: UUID, *, player_id: Optional[UUID] = None, subject_id: Optional[UUID] = None,
    difficulty: Optional[str] = None, match_type: Optional[str] = None, tournament_id: Optional[UUID] = None,
    club_id: Optional[UUID] = None, date_from=None, date_to=None, favorites_only: bool = False,
    page: int = 1, size: int = 20,
) -> Dict[str, Any]:
    from app.models.club import ClubMember

    viewer_club_ids = set(db.exec(
        select(ClubMember.club_id).where(ClubMember.user_id == viewer_id).where(ClubMember.status == "active")
    ).all())

    query = (
        select(CompetitiveMatch, CompetitiveMatchReplay)
        .join(CompetitiveMatchReplay, CompetitiveMatchReplay.match_id == CompetitiveMatch.id)
        .where(CompetitiveMatch.status == "completed")
        .where(CompetitiveMatchReplay.status == "ready")
        .where(CompetitiveMatchReplay.deleted_at.is_(None))
    )
    if difficulty:
        query = query.where(CompetitiveMatch.difficulty == difficulty)
    if match_type:
        query = query.where(CompetitiveMatch.match_type == match_type)
    if date_from:
        query = query.where(CompetitiveMatch.completed_at >= date_from)
    if date_to:
        query = query.where(CompetitiveMatch.completed_at <= date_to)

    rows = db.exec(query.order_by(CompetitiveMatch.completed_at.desc())).all()

    if subject_id:
        rows = [r for r in rows if subject_id in (r[0].subject_ids or [])]

    if favorites_only:
        favorite_ids = set(list_favorite_match_ids(db, viewer_id))
        rows = [r for r in rows if r[0].id in favorite_ids]

    if player_id or tournament_id or club_id:
        from app.models.competitive import CompetitiveMatchParticipant, CompetitiveTournamentMatch
        from app.models.club import ClubBattleMatch
        kept = []
        for match, replay in rows:
            if player_id:
                pids = set(db.exec(select(CompetitiveMatchParticipant.user_id).where(CompetitiveMatchParticipant.match_id == match.id)).all())
                if player_id not in pids:
                    continue
            if tournament_id:
                tm = db.exec(select(CompetitiveTournamentMatch).where(CompetitiveTournamentMatch.match_id == match.id)).first()
                if tm is None or tm.tournament_id != tournament_id:
                    continue
            if club_id:
                cb = db.get(ClubBattleMatch, match.id)
                if cb is None or club_id not in (cb.club_a_id, cb.club_b_id):
                    continue
            kept.append((match, replay))
        rows = kept

    visible = []
    for match, replay in rows:
        if replay.visibility == "public":
            visible.append((match, replay))
        elif replay.visibility == "club" and viewer_club_ids:
            from app.models.competitive import CompetitiveMatchParticipant
            participant_club_ids = set(db.exec(
                select(CompetitiveMatchParticipant.club_id).where(CompetitiveMatchParticipant.match_id == match.id).where(CompetitiveMatchParticipant.club_id.is_not(None))
            ).all())
            if participant_club_ids & viewer_club_ids:
                visible.append((match, replay))

    total = len(visible)
    paged = visible[(page - 1) * size: page * size]
    items = []
    for match, replay in paged:
        participants = match_service.get_participants(db, match.id)
        profiles = {p.id: p for p in db.exec(select(Profile).where(Profile.id.in_([p.user_id for p in participants]))).all()}
        items.append({
            "match_id": str(match.id), "match_type": match.match_type, "difficulty": match.difficulty,
            "completed_at": match.completed_at.isoformat() if match.completed_at else None,
            "visibility": replay.visibility,
            "players": [{"user_id": str(p.user_id), "full_name": profiles[p.user_id].full_name if p.user_id in profiles else None, "score": p.score} for p in participants],
        })
    return {"items": items, "total": total, "page": page, "size": size}
