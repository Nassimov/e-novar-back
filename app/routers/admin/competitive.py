from __future__ import annotations

"""Admin oversight — Competitive Arena Phase 2. Admins can inspect every
duel, force-cancel any of them, and suspend/reinstate a student's access to
the Competitive Arena module specifically (not a platform-wide ban)."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlmodel import Session, func, select

from app.dependencies import get_admin_user, get_db
from app.models.competitive import (
    CompetitiveLeague,
    CompetitiveMatch,
    CompetitiveMatchChat,
    CompetitiveMatchEvent,
    CompetitiveMatchReaction,
    CompetitiveQueueEntry,
    CompetitiveQueueEvent,
    CompetitiveSeason,
    CompetitiveSeasonReward,
    CompetitiveStatistics,
    CompetitiveTournament,
    CompetitiveTournamentParticipant,
)
from app.models.profile import Profile
from app.schemas.competitive import (
    BattleRoyaleCreateRequest,
    BattleRoyaleOut,
    BattleRoyaleParticipantOut,
    BattleRoyaleRewardCreateRequest,
    BattleRoyaleRewardOut,
    BattleRoyaleUpdateRequest,
    BlockedWordCreateRequest,
    BlockedWordOut,
    ChatMuteOut,
    ChatReportOut,
    ChatReportResolveRequest,
    LeagueOut,
    LeagueUpsertRequest,
    LiveMatchListResponse,
    LiveMatchOut,
    LiveMatchParticipantOut,
    MuteUserRequest,
    QueueAdminEntrySummary,
    QueueMonitoringResponse,
    SeasonCreateRequest,
    SeasonOut,
    SeasonRewardCreateRequest,
    SeasonRewardOut,
    SeasonUpdateRequest,
    SpectatorAnalyticsResponse,
    SuspiciousAccountOut,
    TournamentBracketResponse,
    TournamentCreateRequest,
    TournamentOut,
    TournamentParticipantOut,
    TournamentRewardCreateRequest,
    TournamentRewardOut,
    TournamentUpdateRequest,
    UnmuteUserRequest,
)
from app.services.competitive import (
    battle_royale_service,
    event_log_service,
    leaderboard_service,
    match_service,
    ranking_service,
    season_service,
    spectator_presence_service,
    spectator_service,
    statistics_service,
    tournament_service,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Admin — Competitive"])


@router.get("/matches")
def list_all_matches(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    match_type: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    query = select(CompetitiveMatch).where(CompetitiveMatch.deleted_at.is_(None))
    if status_filter:
        query = query.where(CompetitiveMatch.status == status_filter)
    if match_type:
        query = query.where(CompetitiveMatch.match_type == match_type)

    total = db.exec(select(func.count()).select_from(query.subquery())).one()
    matches = list(
        db.exec(query.order_by(CompetitiveMatch.created_at.desc()).offset((page - 1) * size).limit(size)).all()
    )
    items = []
    for m in matches:
        participants = match_service.get_participants(db, m.id)
        items.append({
            "id": str(m.id),
            "match_type": m.match_type,
            "status": m.status,
            "visibility": m.visibility,
            "creator_id": str(m.creator_id) if m.creator_id else None,
            "participants": [str(p.user_id) for p in participants],
            "scheduled_at": m.scheduled_at.isoformat() if m.scheduled_at else None,
            "created_at": m.created_at.isoformat(),
            "updated_at": m.updated_at.isoformat(),
        })
    return {"items": items, "total": total, "page": page, "size": size}


@router.get("/matches/{match_id}")
def get_match_detail(
    match_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    match = match_service.get_match_or_404(db, match_id)
    participants = match_service.get_participants(db, match_id)
    events = event_log_service.list_events(db, match_id)
    return {
        "match": match.model_dump(),
        "participants": [p.model_dump() for p in participants],
        "events": [e.model_dump() for e in events],
    }


class AdminCancelPayload(BaseModel):
    reason: Optional[str] = None


@router.post("/matches/{match_id}/cancel")
def admin_cancel_match(
    match_id: UUID,
    payload: AdminCancelPayload,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    match = match_service.get_match_or_404(db, match_id)
    if match.status in ("completed", "cancelled", "expired", "abandoned"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce match est déjà terminé.")
    match_service.transition(match, "cancelled")
    match.cancelled_at = datetime.now(timezone.utc)
    match.cancelled_reason = payload.reason or "Annulé par un administrateur."
    db.add(match)
    db.commit()

    event_log_service.log_event(
        db, match_id=match.id, actor_id=None, event_type="admin_cancelled",
        meta={"admin_email": _admin.get("email"), "reason": payload.reason},
    )
    logger.info("Admin %s force-cancelled competitive match %s", _admin.get("email"), match_id)
    return {"status": "cancelled", "match_id": str(match_id)}


class SuspendPayload(BaseModel):
    reason: Optional[str] = None
    days: int = 30


@router.post("/students/{user_id}/suspend")
def suspend_student_competitive_access(
    user_id: UUID,
    payload: SuspendPayload,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    profile = db.get(Profile, user_id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Élève introuvable.")
    stats = statistics_service.get_or_create_statistics(db, user_id)
    stats.suspended_until = datetime.now(timezone.utc) + timedelta(days=payload.days)
    stats.suspended_reason = payload.reason
    db.add(stats)
    db.commit()

    logger.info("Admin %s suspended competitive access for user %s (%s days)", _admin.get("email"), user_id, payload.days)
    return {"status": "suspended", "user_id": str(user_id), "suspended_until": stats.suspended_until.isoformat()}


@router.post("/students/{user_id}/reinstate")
def reinstate_student_competitive_access(
    user_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    stats = db.get(CompetitiveStatistics, user_id)
    if stats is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Aucune statistique compétitive pour cet élève.")
    stats.suspended_until = None
    stats.suspended_reason = None
    db.add(stats)
    db.commit()

    logger.info("Admin %s reinstated competitive access for user %s", _admin.get("email"), user_id)
    return {"status": "reinstated", "user_id": str(user_id)}


# ─── Phase 6 — Matchmaking Queue Monitoring ─────────────────────────────────

@router.get("/queue/monitoring", response_model=QueueMonitoringResponse)
def get_queue_monitoring(
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Everything here is computed live from competitive_queue_entries /
    competitive_queue_events — no separate pre-aggregated statistics table
    (see migration 079's header comment for why)."""
    now = datetime.now(timezone.utc)
    since_hour = now - timedelta(hours=1)

    active_rows = list(
        db.exec(
            select(CompetitiveQueueEntry)
            .where(CompetitiveQueueEntry.status.in_(("waiting", "searching", "matched", "accepted")))
            .where(CompetitiveQueueEntry.match_id.is_(None))
            .order_by(CompetitiveQueueEntry.search_started_at.asc())
        ).all()
    )

    searching_rows = [e for e in active_rows if e.status == "searching"]
    queue_length_by_mode: Dict[str, int] = {}
    for e in searching_rows:
        queue_length_by_mode[e.mode] = queue_length_by_mode.get(e.mode, 0) + 1

    avg_wait = (
        sum((now - e.search_started_at).total_seconds() for e in searching_rows) / len(searching_rows)
        if searching_rows else 0.0
    )

    matches_created = db.exec(
        select(func.count()).select_from(CompetitiveQueueEvent)
        .where(CompetitiveQueueEvent.event_type == "queue_match_created")
        .where(CompetitiveQueueEvent.created_at >= since_hour)
    ).one()
    cancelled = db.exec(
        select(func.count()).select_from(CompetitiveQueueEvent)
        .where(CompetitiveQueueEvent.event_type == "queue_left")
        .where(CompetitiveQueueEvent.created_at >= since_hour)
    ).one()
    expired = db.exec(
        select(func.count()).select_from(CompetitiveQueueEvent)
        .where(CompetitiveQueueEvent.event_type == "queue_proposal_expired")
        .where(CompetitiveQueueEvent.created_at >= since_hour)
    ).one()

    entries = [
        QueueAdminEntrySummary(
            queue_entry_id=e.id,
            user_id=e.user_id,
            mode=e.mode,
            subject_id=e.subject_id,
            school_level_id=e.school_level_id,
            difficulty=e.difficulty,
            status=e.status,
            elapsed_seconds=int((now - e.search_started_at).total_seconds()),
            current_radius=e.current_radius,
        )
        for e in active_rows
    ]

    return QueueMonitoringResponse(
        players_waiting=len(searching_rows),
        avg_wait_seconds=round(avg_wait, 1),
        queue_length_by_mode=queue_length_by_mode,
        active_proposals=len([e for e in active_rows if e.status in ("matched", "accepted")]) // 2,
        matches_created_last_hour=matches_created,
        cancelled_searches_last_hour=cancelled,
        expired_searches_last_hour=expired,
        entries=entries,
    )


# ─── Phase 7 — League Ladder Admin ──────────────────────────────────────────

@router.get("/leagues", response_model=List[LeagueOut])
def list_leagues(
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    return ranking_service.list_leagues(db)


@router.post("/leagues", response_model=LeagueOut)
def create_league(
    payload: LeagueUpsertRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    league = CompetitiveLeague(**payload.model_dump())
    db.add(league)
    db.commit()
    db.refresh(league)
    logger.info("Admin %s created competitive league id=%s name=%s %s", _admin.get("email"), league.id, league.league_name, league.division_label)
    return league


@router.patch("/leagues/{league_id}", response_model=LeagueOut)
def update_league(
    league_id: UUID,
    payload: LeagueUpsertRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    league = db.get(CompetitiveLeague, league_id)
    if league is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ligue introuvable.")
    for key, value in payload.model_dump().items():
        setattr(league, key, value)
    league.updated_at = datetime.now(timezone.utc)
    db.add(league)
    db.commit()
    db.refresh(league)
    logger.info("Admin %s updated competitive league id=%s", _admin.get("email"), league.id)
    return league


# ─── Phase 7 — Season Manager Admin ──────────────────────────────────────────

@router.get("/seasons", response_model=List[SeasonOut])
def list_seasons(
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    return season_service.list_seasons(db)


@router.post("/seasons", response_model=SeasonOut)
def create_season(
    payload: SeasonCreateRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    season = season_service.create_season(
        db,
        name=payload.name, description=payload.description,
        starts_at=payload.starts_at, ends_at=payload.ends_at,
        reset_strategy=payload.reset_strategy, reset_percentage=payload.reset_percentage,
        rules=payload.rules, activate_now=payload.activate_now,
    )
    logger.info("Admin %s created competitive season id=%s activate_now=%s", _admin.get("email"), season.id, payload.activate_now)
    return season


@router.patch("/seasons/{season_id}", response_model=SeasonOut)
def update_season(
    season_id: UUID,
    payload: SeasonUpdateRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    season = season_service.update_season(db, season_id, **payload.model_dump())
    logger.info("Admin %s updated competitive season id=%s", _admin.get("email"), season.id)
    return season


@router.post("/seasons/{season_id}/activate", response_model=SeasonOut)
def activate_season(
    season_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    season = season_service.activate_season(db, season_id)
    logger.info("Admin %s activated competitive season id=%s", _admin.get("email"), season.id)
    return season


@router.post("/seasons/{season_id}/end", response_model=SeasonOut)
def end_season(
    season_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    season = season_service.end_season(db, season_id)
    logger.info("Admin %s manually ended competitive season id=%s", _admin.get("email"), season.id)
    return season


@router.get("/seasons/{season_id}/rewards", response_model=List[SeasonRewardOut])
def list_season_rewards(
    season_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    season_service.get_season_or_404(db, season_id)
    return list(
        db.exec(select(CompetitiveSeasonReward).where(CompetitiveSeasonReward.season_id == season_id)).all()
    )


@router.post("/seasons/{season_id}/rewards", response_model=SeasonRewardOut)
def create_season_reward(
    season_id: UUID,
    payload: SeasonRewardCreateRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    season_service.get_season_or_404(db, season_id)
    reward = CompetitiveSeasonReward(season_id=season_id, **payload.model_dump())
    db.add(reward)
    db.commit()
    db.refresh(reward)
    logger.info("Admin %s created season reward id=%s for season=%s", _admin.get("email"), reward.id, season_id)
    return reward


# ─── Phase 7 — Leaderboard cache / Ranking maintenance ──────────────────────

@router.post("/leaderboard/invalidate")
def invalidate_leaderboard_cache(
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    leaderboard_service.invalidate_all()
    logger.info("Admin %s invalidated the competitive leaderboard cache", _admin.get("email"))
    return {"status": "ok"}


@router.post("/ranking/recalculate")
def recalculate_ranking(
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Resyncs every competitive_statistics row's derived league_id/rank_tier
    from its current (untouched) rating — for when an admin edits the league
    ladder's thresholds and existing players' derived league needs to catch
    up (mirrors migration 082's own idempotent resync UPDATE, at the ORM
    layer, on demand). Fetches the ladder once and reuses it for every row
    (see ranking_service.get_league_for_rating's `leagues` param) instead of
    re-querying it per user; competitive_statistics is one row per student
    so a single full-table pass — no chunking — matches the codebase's
    existing convention for this table (see migration 082's own comment:
    "cheap to re-run — one row per student")."""
    leagues = ranking_service.list_leagues(db)
    all_stats = list(db.exec(select(CompetitiveStatistics)).all())

    updated = 0
    for stats in all_stats:
        league = ranking_service.get_league_for_rating(db, stats.rating, leagues=leagues)
        new_league_id = league.id if league else None
        new_tier = ranking_service.league_slug(league)
        if stats.league_id != new_league_id or stats.rank_tier != new_tier:
            stats.league_id = new_league_id
            stats.rank_tier = new_tier
            db.add(stats)
            updated += 1
    db.commit()

    logger.info("Admin %s triggered ranking recalculation: users_updated=%s (of %s total)", _admin.get("email"), updated, len(all_stats))
    return {"status": "ok", "users_updated": updated}


# ─── Phase 7 — Anti-Abuse Analyzer dashboard ────────────────────────────────

@router.get("/anti-abuse/suspicious", response_model=List[SuspiciousAccountOut])
def list_suspicious_accounts(
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Groups every anti_abuse_* CompetitiveMatchEvent (see
    app/services/competitive/anti_abuse_service.py) by actor, most recent
    signal first."""
    base = (
        select(
            CompetitiveMatchEvent.actor_id,
            func.count().label("signal_count"),
            func.max(CompetitiveMatchEvent.created_at).label("last_signal_at"),
        )
        .where(CompetitiveMatchEvent.event_type.like("anti_abuse_%"))
        .where(CompetitiveMatchEvent.actor_id.is_not(None))
        .group_by(CompetitiveMatchEvent.actor_id)
    )
    rows = list(
        db.exec(
            base.order_by(func.max(CompetitiveMatchEvent.created_at).desc())
            .offset((page - 1) * size).limit(size)
        ).all()
    )
    if not rows:
        return []

    actor_ids = [r[0] for r in rows]
    type_rows = list(
        db.exec(
            select(CompetitiveMatchEvent.actor_id, CompetitiveMatchEvent.event_type)
            .where(CompetitiveMatchEvent.actor_id.in_(actor_ids))
            .where(CompetitiveMatchEvent.event_type.like("anti_abuse_%"))
            .distinct()
        ).all()
    )
    types_by_actor: Dict[UUID, List[str]] = {}
    for actor_id, event_type in type_rows:
        types_by_actor.setdefault(actor_id, []).append(event_type)

    profiles = {p.id: p for p in db.exec(select(Profile).where(Profile.id.in_(actor_ids))).all()}

    return [
        SuspiciousAccountOut(
            user_id=actor_id,
            full_name=profiles[actor_id].full_name if actor_id in profiles else None,
            signal_types=sorted(types_by_actor.get(actor_id, [])),
            signal_count=signal_count,
            last_signal_at=last_signal_at,
        )
        for actor_id, signal_count, last_signal_at in rows
    ]


# ─── Phase 8 — Spectator Mode, Live Reactions & Predictions Admin ───────────

@router.get("/live-matches")
def admin_list_live_matches(
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Admin view of every currently in_progress match (public AND private
    — unlike the player-facing GET /api/competitive/live-matches, which only
    surfaces public ones), enriched with chat/reaction volume and a live
    spectator count. "Peak" spectator count is NOT separately tracked (would
    need a new time-series table for a metric that's mostly a "nice to
    know") — the CURRENT live count is used as a reasonable proxy, same
    simplification the player-facing endpoint's "trending" sort makes."""
    matches = list(db.exec(select(CompetitiveMatch).where(CompetitiveMatch.status == "in_progress")).all())
    total = len(matches)
    paged = matches[(page - 1) * size: page * size]

    items = []
    for match in paged:
        participants = match_service.get_participants(db, match.id)
        players = [
            LiveMatchParticipantOut(
                user_id=p.user_id,
                full_name=(db.get(Profile, p.user_id) or Profile(id=p.user_id)).full_name,
                avatar_url=(db.get(Profile, p.user_id) or Profile(id=p.user_id)).avatar_url,
                score=p.score,
            )
            for p in participants
        ]
        chat_volume = db.exec(select(func.count()).select_from(CompetitiveMatchChat).where(CompetitiveMatchChat.match_id == match.id)).one()
        reaction_volume = db.exec(select(func.count()).select_from(CompetitiveMatchReaction).where(CompetitiveMatchReaction.match_id == match.id)).one()
        spectator_count = spectator_presence_service.get_count(match.id)
        score_delta = abs(players[0].score - players[1].score) if len(players) == 2 else None
        item = LiveMatchOut(
            match_id=match.id, match_type=match.match_type, subject_ids=match.subject_ids,
            school_level_id=match.school_level_id, difficulty=match.difficulty,
            question_count=match.question_count, current_position=match.current_question_position,
            started_at=match.started_at,
            duration_sec=int((datetime.now(timezone.utc) - match.started_at).total_seconds()) if match.started_at else None,
            spectator_count=spectator_count, players=players, score_delta=score_delta,
        )
        items.append(item.model_dump() | {"chat_volume": chat_volume, "reaction_volume": reaction_volume, "peak_spectator_count_estimate": spectator_count})

    return {"items": items, "total": total, "page": page, "size": size}


@router.get("/chat/muted", response_model=List[ChatMuteOut])
def list_muted_users(
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    return spectator_service.list_active_mutes(db)


@router.post("/chat/mute", response_model=ChatMuteOut)
def mute_chat_user(
    payload: MuteUserRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    mute = spectator_service.mute_user(
        db, user_id=payload.user_id, muted_by=None, duration_minutes=payload.duration_minutes, reason=payload.reason,
    )
    logger.info("Admin %s muted competitive chat user=%s duration_minutes=%s", _admin.get("email"), payload.user_id, payload.duration_minutes)
    profile = db.get(Profile, payload.user_id)
    return {
        "id": mute.id, "user_id": mute.user_id, "full_name": profile.full_name if profile else None,
        "muted_by": mute.muted_by, "reason": mute.reason, "muted_until": mute.muted_until, "created_at": mute.created_at,
    }


@router.post("/chat/unmute")
def unmute_chat_user(
    payload: UnmuteUserRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    count = spectator_service.unmute_user(db, user_id=payload.user_id)
    logger.info("Admin %s unmuted competitive chat user=%s (%s mutes ended)", _admin.get("email"), payload.user_id, count)
    return {"status": "unmuted", "user_id": str(payload.user_id), "mutes_ended": count}


@router.get("/chat/blocked-words", response_model=List[BlockedWordOut])
def list_blocked_words(
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    return spectator_service.list_blocked_words(db)


@router.post("/chat/blocked-words", response_model=BlockedWordOut)
def add_blocked_word(
    payload: BlockedWordCreateRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    word = spectator_service.add_blocked_word(db, word=payload.word, created_by=None)
    logger.info("Admin %s added blocked word id=%s", _admin.get("email"), word.id)
    return word


@router.delete("/chat/blocked-words/{word_id}")
def delete_blocked_word(
    word_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    spectator_service.delete_blocked_word(db, word_id=word_id)
    logger.info("Admin %s deleted blocked word id=%s", _admin.get("email"), word_id)
    return {"status": "deleted"}


@router.get("/chat/reports", response_model=List[ChatReportOut])
def list_chat_reports(
    resolved: Optional[bool] = Query(default=False),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    items, _total = spectator_service.list_chat_reports(db, resolved=resolved, page=page, size=size)
    return items


@router.post("/chat/reports/{report_id}/resolve", response_model=ChatReportOut)
def resolve_chat_report(
    report_id: UUID,
    payload: ChatReportResolveRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    report = spectator_service.resolve_chat_report(db, report_id=report_id, action=payload.action)
    logger.info("Admin %s resolved chat report id=%s action=%s", _admin.get("email"), report_id, payload.action)
    return {
        "id": report.id, "message_id": report.message_id, "message_text": None,
        "reporter_id": report.reporter_id, "reporter_name": None,
        "reason": report.reason, "resolved_at": report.resolved_at, "resolved_by": report.resolved_by,
        "created_at": report.created_at,
    }


@router.delete("/chat/{match_id}/messages/{message_id}")
def admin_delete_chat_message(
    match_id: UUID,
    message_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    spectator_service.admin_delete_chat_message(db, match_id=match_id, message_id=message_id)
    logger.info("Admin %s deleted competitive chat message id=%s match=%s", _admin.get("email"), message_id, match_id)
    return {"status": "deleted"}


@router.get("/spectator-analytics", response_model=SpectatorAnalyticsResponse)
def get_spectator_analytics(
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    live_matches = list(db.exec(select(CompetitiveMatch).where(CompetitiveMatch.status == "in_progress")).all())
    counts = spectator_presence_service.get_counts([m.id for m in live_matches])
    total = sum(counts.values())
    avg = round(total / len(live_matches), 2) if live_matches else 0.0
    peak_estimate = max(counts.values(), default=0)
    return SpectatorAnalyticsResponse(
        live_matches=len(live_matches), current_total_spectators=total,
        peak_spectators_estimate=peak_estimate, avg_spectators_per_match=avg,
    )


# ─── Phase 9 — Tournament System (Single Elimination), Part A — Admin ───────

def _tournament_to_out(db: Session, tournament: CompetitiveTournament) -> TournamentOut:
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


@router.post("/tournaments", response_model=TournamentOut)
def create_tournament(
    payload: TournamentCreateRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    tournament = tournament_service.create_tournament(db, organizer_id=None, actor_email=_admin.get("email"), **payload.model_dump())
    logger.info("Admin %s created tournament id=%s name=%s", _admin.get("email"), tournament.id, tournament.name)
    return _tournament_to_out(db, tournament)


@router.get("/tournaments", response_model=List[TournamentOut])
def list_tournaments(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    tournaments = tournament_service.list_tournaments_admin(db, status_filter=status_filter)
    return [_tournament_to_out(db, t) for t in tournaments]


@router.get("/tournaments/{tournament_id}", response_model=TournamentOut)
def get_tournament(
    tournament_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    tournament = tournament_service.get_tournament_or_404(db, tournament_id)
    return _tournament_to_out(db, tournament)


@router.patch("/tournaments/{tournament_id}", response_model=TournamentOut)
def update_tournament(
    tournament_id: UUID,
    payload: TournamentUpdateRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    tournament = tournament_service.update_tournament(db, tournament_id, actor_email=_admin.get("email"), **payload.model_dump())
    return _tournament_to_out(db, tournament)


@router.post("/tournaments/{tournament_id}/publish", response_model=TournamentOut)
def publish_tournament(
    tournament_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    tournament = tournament_service.publish_tournament(db, tournament_id, actor_email=_admin.get("email"))
    logger.info("Admin %s published tournament id=%s", _admin.get("email"), tournament_id)
    return _tournament_to_out(db, tournament)


@router.post("/tournaments/{tournament_id}/open-registration", response_model=TournamentOut)
def open_tournament_registration(
    tournament_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    tournament = tournament_service.open_registration(db, tournament_id, actor_email=_admin.get("email"))
    logger.info("Admin %s opened registration for tournament id=%s", _admin.get("email"), tournament_id)
    return _tournament_to_out(db, tournament)


@router.post("/tournaments/{tournament_id}/close-registration", response_model=TournamentOut)
def close_tournament_registration(
    tournament_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    tournament = tournament_service.close_registration(db, tournament_id, actor_email=_admin.get("email"))
    logger.info("Admin %s closed registration for tournament id=%s", _admin.get("email"), tournament_id)
    return _tournament_to_out(db, tournament)


@router.post("/tournaments/{tournament_id}/generate-bracket", response_model=TournamentBracketResponse)
def generate_tournament_bracket(
    tournament_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    tournament = tournament_service.get_tournament_or_404(db, tournament_id)
    tournament = tournament_service.generate_bracket(db, tournament)
    logger.info("Admin %s generated bracket for tournament id=%s", _admin.get("email"), tournament_id)
    return tournament_service.get_bracket_tree(db, tournament)


class AdminTournamentCancelPayload(BaseModel):
    reason: Optional[str] = None


@router.post("/tournaments/{tournament_id}/cancel", response_model=TournamentOut)
def cancel_tournament(
    tournament_id: UUID,
    payload: AdminTournamentCancelPayload,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    tournament = tournament_service.cancel_tournament(db, tournament_id, actor_email=_admin.get("email"), reason=payload.reason)
    logger.info("Admin %s cancelled tournament id=%s", _admin.get("email"), tournament_id)
    return _tournament_to_out(db, tournament)


@router.post("/tournaments/{tournament_id}/pause", response_model=TournamentOut)
def pause_tournament(
    tournament_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    tournament = tournament_service.pause_tournament(db, tournament_id, actor_email=_admin.get("email"))
    logger.info("Admin %s paused tournament id=%s", _admin.get("email"), tournament_id)
    return _tournament_to_out(db, tournament)


@router.post("/tournaments/{tournament_id}/resume", response_model=TournamentOut)
def resume_tournament(
    tournament_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    tournament = tournament_service.resume_tournament(db, tournament_id, actor_email=_admin.get("email"))
    logger.info("Admin %s resumed tournament id=%s", _admin.get("email"), tournament_id)
    return _tournament_to_out(db, tournament)


@router.get("/tournaments/{tournament_id}/participants", response_model=List[TournamentParticipantOut])
def admin_list_tournament_participants(
    tournament_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    tournament_service.get_tournament_or_404(db, tournament_id)
    return tournament_service.list_participants(db, tournament_id)


@router.get("/tournaments/{tournament_id}/rewards", response_model=List[TournamentRewardOut])
def list_tournament_rewards(
    tournament_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    tournament_service.get_tournament_or_404(db, tournament_id)
    return tournament_service.list_rewards(db, tournament_id)


@router.post("/tournaments/{tournament_id}/rewards", response_model=TournamentRewardOut)
def create_tournament_reward(
    tournament_id: UUID,
    payload: TournamentRewardCreateRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    reward = tournament_service.create_reward(db, tournament_id, actor_email=_admin.get("email"), **payload.model_dump())
    logger.info("Admin %s created tournament reward id=%s for tournament=%s", _admin.get("email"), reward.id, tournament_id)
    return reward


@router.delete("/tournaments/{tournament_id}/rewards/{reward_id}")
def delete_tournament_reward(
    tournament_id: UUID,
    reward_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    tournament_service.delete_reward(db, tournament_id, reward_id, actor_email=_admin.get("email"))
    logger.info("Admin %s deleted tournament reward id=%s for tournament=%s", _admin.get("email"), reward_id, tournament_id)
    return {"status": "deleted"}


# ─── Phase 9 — Tournament System (Single Elimination), Part B — Admin ───────

@router.post("/tournaments/{tournament_id}/start", response_model=TournamentOut)
def start_tournament(
    tournament_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """The missing link between 'bracket_generated' and real gameplay —
    transitions the tournament to 'running' and kicks off round 1 (which
    itself creates the real duel for every 'ready' match, cascading past any
    fully-bye round automatically)."""
    tournament = tournament_service.start_tournament(db, tournament_id, actor_email=_admin.get("email"))
    logger.info("Admin %s started tournament id=%s", _admin.get("email"), tournament_id)
    return _tournament_to_out(db, tournament)


class TournamentMatchDisqualifyPayload(BaseModel):
    user_id: UUID
    reason: Optional[str] = None


@router.post("/tournaments/{tournament_id}/matches/{tournament_match_id}/disqualify")
def disqualify_tournament_participant(
    tournament_id: UUID,
    tournament_match_id: UUID,
    payload: TournamentMatchDisqualifyPayload,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    tm = tournament_service.admin_disqualify_participant(
        db, tournament_id, tournament_match_id, user_id=payload.user_id, reason=payload.reason,
        actor_email=_admin.get("email"),
    )
    logger.info(
        "Admin %s disqualified user=%s from tournament=%s tournament_match=%s",
        _admin.get("email"), payload.user_id, tournament_id, tournament_match_id,
    )
    return tm.model_dump()


class TournamentMatchReplacePayload(BaseModel):
    old_user_id: UUID
    new_user_id: UUID


@router.post("/tournaments/{tournament_id}/matches/{tournament_match_id}/replace")
def replace_tournament_participant(
    tournament_id: UUID,
    tournament_match_id: UUID,
    payload: TournamentMatchReplacePayload,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    tm = tournament_service.admin_replace_participant(
        db, tournament_id, tournament_match_id, old_user_id=payload.old_user_id, new_user_id=payload.new_user_id,
        actor_email=_admin.get("email"),
    )
    logger.info(
        "Admin %s replaced user=%s with user=%s in tournament=%s tournament_match=%s",
        _admin.get("email"), payload.old_user_id, payload.new_user_id, tournament_id, tournament_match_id,
    )
    return tm.model_dump()


class TournamentMatchAdvancePayload(BaseModel):
    winner_id: UUID


@router.post("/tournaments/{tournament_id}/matches/{tournament_match_id}/advance")
def advance_tournament_match(
    tournament_id: UUID,
    tournament_match_id: UUID,
    payload: TournamentMatchAdvancePayload,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Admin override for disputes/edge cases where the real match can't
    cleanly determine a winner — advances the bracket directly."""
    tm = tournament_service.admin_advance_match(
        db, tournament_id, tournament_match_id, winner_id=payload.winner_id, actor_email=_admin.get("email"),
    )
    logger.info(
        "Admin %s manually advanced winner=%s for tournament=%s tournament_match=%s",
        _admin.get("email"), payload.winner_id, tournament_id, tournament_match_id,
    )
    return tm.model_dump()


@router.post("/tournaments/{tournament_id}/matches/{tournament_match_id}/restart")
def restart_tournament_match(
    tournament_id: UUID,
    tournament_match_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    tm = tournament_service.admin_restart_match(db, tournament_id, tournament_match_id, actor_email=_admin.get("email"))
    logger.info("Admin %s restarted tournament_match=%s for tournament=%s", _admin.get("email"), tournament_match_id, tournament_id)
    return tm.model_dump()


@router.get("/tournaments-analytics")
def get_tournaments_analytics(
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Platform-wide (NOT scoped to a single tournament) — registrations,
    participation/completion rates, average duration, most popular subjects/
    levels, peak spectators, rewards distributed, aggregated across every
    tournament ever created."""
    return tournament_service.get_tournaments_analytics(db)


# ─── Phase 10 — Battle Royale, Part A — Admin ───────────────────────────────

def _battle_royale_to_out(db: Session, match: CompetitiveMatch, br) -> BattleRoyaleOut:
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


@router.post("/battle-royales", response_model=BattleRoyaleOut)
def admin_create_battle_royale(
    payload: BattleRoyaleCreateRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    match = battle_royale_service.create_battle_royale_match(db, actor_email=_admin.get("email"), **payload.model_dump())
    _, br = battle_royale_service.get_battle_royale_or_404(db, match.id)
    logger.info("Admin %s created battle royale id=%s max_players=%s", _admin.get("email"), match.id, br.max_players)
    return _battle_royale_to_out(db, match, br)


@router.get("/battle-royales", response_model=List[BattleRoyaleOut])
def admin_list_battle_royales(
    phase: Optional[str] = Query(default=None),
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    pairs = battle_royale_service.list_battle_royales_admin(db, phase_filter=phase)
    return [_battle_royale_to_out(db, m, br) for m, br in pairs]


@router.get("/battle-royales/{match_id}", response_model=BattleRoyaleOut)
def admin_get_battle_royale(
    match_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    match, br = battle_royale_service.get_battle_royale_or_404(db, match_id)
    return _battle_royale_to_out(db, match, br)


@router.patch("/battle-royales/{match_id}", response_model=BattleRoyaleOut)
def admin_update_battle_royale(
    match_id: UUID,
    payload: BattleRoyaleUpdateRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    match, br = battle_royale_service.get_battle_royale_or_404(db, match_id)
    match, br = battle_royale_service.update_battle_royale(db, match, br, actor_email=_admin.get("email"), **payload.model_dump())
    return _battle_royale_to_out(db, match, br)


class BattleRoyaleCancelPayload(BaseModel):
    reason: Optional[str] = None


@router.post("/battle-royales/{match_id}/cancel", response_model=BattleRoyaleOut)
def admin_cancel_battle_royale(
    match_id: UUID,
    payload: BattleRoyaleCancelPayload,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    match, br = battle_royale_service.get_battle_royale_or_404(db, match_id)
    match = battle_royale_service.cancel_battle_royale(db, match, br, actor_email=_admin.get("email"), reason=payload.reason)
    logger.info("Admin %s cancelled battle royale id=%s", _admin.get("email"), match_id)
    return _battle_royale_to_out(db, match, br)


@router.post("/battle-royales/{match_id}/force-start", response_model=BattleRoyaleOut)
def admin_force_start_battle_royale(
    match_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    match, br = battle_royale_service.get_battle_royale_or_404(db, match_id)
    match = battle_royale_service.force_start_battle_royale(db, match, br, actor_email=_admin.get("email"))
    logger.info("Admin %s force-started battle royale id=%s", _admin.get("email"), match_id)
    return _battle_royale_to_out(db, match, br)


@router.post("/battle-royales/{match_id}/pause", response_model=BattleRoyaleOut)
def admin_pause_battle_royale(
    match_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    match, br = battle_royale_service.get_battle_royale_or_404(db, match_id)
    match = battle_royale_service.pause_battle_royale(db, match, br, actor_email=_admin.get("email"))
    logger.info("Admin %s paused battle royale id=%s", _admin.get("email"), match_id)
    return _battle_royale_to_out(db, match, br)


@router.post("/battle-royales/{match_id}/resume", response_model=BattleRoyaleOut)
def admin_resume_battle_royale(
    match_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    match, br = battle_royale_service.get_battle_royale_or_404(db, match_id)
    match = battle_royale_service.resume_battle_royale(db, match, br, actor_email=_admin.get("email"))
    logger.info("Admin %s resumed battle royale id=%s", _admin.get("email"), match_id)
    return _battle_royale_to_out(db, match, br)


@router.get("/battle-royales/{match_id}/participants", response_model=List[BattleRoyaleParticipantOut])
def admin_list_battle_royale_participants(
    match_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    battle_royale_service.get_battle_royale_or_404(db, match_id)
    participants = battle_royale_service.admin_list_participants(db, match_id)
    profiles = {p.id: p for p in db.exec(select(Profile).where(Profile.id.in_({pp.user_id for pp in participants}))).all()} if participants else {}
    return [
        BattleRoyaleParticipantOut(
            id=p.id, match_id=p.match_id, user_id=p.user_id,
            full_name=profiles[p.user_id].full_name if p.user_id in profiles else None,
            avatar_url=profiles[p.user_id].avatar_url if p.user_id in profiles else None,
            is_ready=p.is_ready, score=p.score, lives=p.lives, elimination_round=p.elimination_round,
            final_rank=p.final_rank, eliminated_at=p.eliminated_at, joined_at=p.joined_at, left_at=p.left_at,
        )
        for p in participants
    ]


@router.get("/battle-royales/{match_id}/rewards", response_model=List[BattleRoyaleRewardOut])
def admin_list_battle_royale_rewards(
    match_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    battle_royale_service.get_battle_royale_or_404(db, match_id)
    return battle_royale_service.list_rewards(db, match_id)


@router.post("/battle-royales/{match_id}/rewards", response_model=BattleRoyaleRewardOut)
def admin_create_battle_royale_reward(
    match_id: UUID,
    payload: BattleRoyaleRewardCreateRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    battle_royale_service.get_battle_royale_or_404(db, match_id)
    reward = battle_royale_service.create_reward(db, match_id, actor_email=_admin.get("email"), **payload.model_dump())
    logger.info("Admin %s created battle royale reward id=%s for match=%s", _admin.get("email"), reward.id, match_id)
    return reward


@router.delete("/battle-royales/{match_id}/rewards/{reward_id}")
def admin_delete_battle_royale_reward(
    match_id: UUID,
    reward_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    battle_royale_service.delete_reward(db, match_id, reward_id, actor_email=_admin.get("email"))
    logger.info("Admin %s deleted battle royale reward id=%s for match=%s", _admin.get("email"), reward_id, match_id)
    return {"status": "deleted"}


# ─── Phase 10, Part C — Battle Royale admin live-match actions ─────────────

class BattleRoyaleKickPayload(BaseModel):
    user_id: UUID


@router.post("/battle-royales/{match_id}/kick")
def admin_kick_battle_royale_participant(
    match_id: UUID,
    payload: BattleRoyaleKickPayload,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Remove a player from an active (not yet started, i.e. 'waiting_room')
    lobby — no elimination bookkeeping (they weren't playing yet). Use
    /disqualify instead once the match is running."""
    match, br = battle_royale_service.get_battle_royale_or_404(db, match_id)
    battle_royale_service.admin_kick_participant(db, match, br, user_id=payload.user_id, actor_email=_admin.get("email"))
    logger.info("Admin %s kicked user=%s from battle royale=%s", _admin.get("email"), payload.user_id, match_id)
    return {"status": "kicked"}


class BattleRoyaleDisqualifyPayload(BaseModel):
    user_id: UUID
    reason: Optional[str] = None


@router.post("/battle-royales/{match_id}/disqualify")
def admin_disqualify_battle_royale_participant(
    match_id: UUID,
    payload: BattleRoyaleDisqualifyPayload,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """For a match already running/final_phase/final_duel — same elimination
    bookkeeping as a normal per-question elimination."""
    match, br = battle_royale_service.get_battle_royale_or_404(db, match_id)
    battle_royale_service.admin_disqualify_participant(
        db, match, br, user_id=payload.user_id, reason=payload.reason, actor_email=_admin.get("email"),
    )
    logger.info("Admin %s disqualified user=%s from battle royale=%s", _admin.get("email"), payload.user_id, match_id)
    return {"status": "disqualified"}


@router.post("/battle-royales/{match_id}/restart", response_model=BattleRoyaleOut)
def admin_restart_battle_royale(
    match_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """No bracket slot exists to reset (unlike a tournament match) — cancels
    the current match outright; create a fresh Battle Royale via
    POST /battle-royales to relaunch."""
    match, br = battle_royale_service.get_battle_royale_or_404(db, match_id)
    match = battle_royale_service.admin_restart_battle_royale(db, match, br, actor_email=_admin.get("email"))
    logger.info("Admin %s restarted (cancelled) battle royale=%s", _admin.get("email"), match_id)
    return _battle_royale_to_out(db, match, br)


@router.get("/battle-royales/{match_id}/monitor")
def admin_monitor_battle_royale(
    match_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Live admin monitoring view: current phase, remaining count, full
    leaderboard snapshot, recent elimination events, spectator count."""
    match, br = battle_royale_service.get_battle_royale_or_404(db, match_id)
    return battle_royale_service.get_admin_monitor(db, match, br)


@router.get("/battle-royales-analytics")
def get_battle_royales_analytics(
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Platform-wide (NOT scoped to a single match) — total matches, average
    duration, average/peak players per match, question accuracy, dropout
    rate, spectator counts, reward distribution counts, most popular
    subjects, aggregated across every Battle Royale ever created."""
    return battle_royale_service.get_battle_royales_analytics(db)


# ─── Phase 12 — Replay administration ───────────────────────────────────────
# Deletion/visibility-lock are admin-only (spec: "Only administrators may
# override" replay privacy; the same reasoning extends to deletion — see
# replay_service.delete_replay's own docstring for why this isn't left to
# either duel participant unilaterally).

@router.post("/replays/{match_id}/delete")
def admin_delete_replay(
    match_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    from app.services.competitive import replay_service
    replay_service.delete_replay(db, match_id, actor_id=None)
    logger.info("Admin %s deleted replay match=%s", _admin.get("email"), match_id)
    return {"deleted": True}


@router.post("/replays/{match_id}/restore")
def admin_restore_replay(
    match_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    from app.services.competitive import replay_service
    replay_service.restore_replay(db, match_id)
    logger.info("Admin %s restored replay match=%s", _admin.get("email"), match_id)
    return {"restored": True}


@router.get("/ranking-analytics")
def get_ranking_analytics(
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    from app.services.competitive import ranking_analytics_service
    return ranking_analytics_service.get_ranking_analytics(db)


@router.post("/students/{user_id}/fair-play/adjust")
def admin_adjust_fair_play(
    user_id: UUID,
    delta: int = Query(...),
    reason: str = Query(...),
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    from app.services.competitive import fair_play_service
    fair_play_service.award(db, user_id, delta=delta, reason=f"admin_adjustment: {reason}")
    logger.info("Admin %s adjusted fair play delta=%s user=%s reason=%s", _admin.get("email"), delta, user_id, reason)
    stats = db.get(CompetitiveStatistics, user_id)
    return {"fair_play_score": stats.fair_play_score if stats else None}


@router.post("/replays/{match_id}/visibility")
def admin_set_replay_visibility(
    match_id: UUID,
    visibility: str = Query(...),
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Admin override — locks the visibility so the owner can no longer
    change it back (replay_service.set_replay_visibility's is_admin=True path)."""
    from app.services.competitive import match_service as _match_service
    from app.services.competitive import replay_service
    match = _match_service.get_match_or_404(db, match_id)
    replay = replay_service.set_replay_visibility(db, match, None, visibility, is_admin=True)
    logger.info("Admin %s set replay visibility=%s match=%s", _admin.get("email"), visibility, match_id)
    return {"visibility": replay.visibility, "locked": replay.visibility_locked}


# ─── Phase 14 — Achievements, Titles & Cosmetics admin panel ────────────────
# Spec: "Administrator can manage: Achievement Categories, Achievement Rules,
# Reward Rules, Titles, Badges, Frames, Banners, Profile Effects,
# Collections, Rarity, Hidden Achievements, Season Rewards, Event Rewards."
# Badges/cosmetics/collections have no dedicated admin CRUD anywhere else in
# the codebase (student_badges.py is player-facing/read-only) — all new here.

class BadgeUpsertRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    tier: Optional[str] = None
    category: Optional[str] = None
    condition_type: Optional[str] = None
    condition_threshold: Optional[int] = None
    ep_reward: Optional[int] = None
    sort_order: Optional[int] = None
    active: Optional[bool] = None
    rarity: Optional[str] = None
    is_hidden: Optional[bool] = None
    reward_config: Optional[List[Dict[str, Any]]] = None
    event_key: Optional[str] = None
    available_from: Optional[datetime] = None
    available_until: Optional[datetime] = None


@router.get("/badges")
def admin_list_badges(
    category: Optional[str] = Query(default=None),
    arena_only: bool = Query(default=True),
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    from app.models.gamification import Badge
    from app.services.competitive import arena_achievement_service
    query = select(Badge)
    if arena_only:
        query = query.where(Badge.condition_type.in_(arena_achievement_service.ARENA_CONDITION_TYPES))
    if category:
        query = query.where(Badge.category == category)
    rows = list(db.exec(query.order_by(Badge.category.asc(), Badge.sort_order.asc())).all())
    return {"items": rows}


@router.post("/badges")
def admin_create_badge(
    payload: BadgeUpsertRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    from app.models.gamification import Badge
    if not payload.name or not payload.condition_type or payload.condition_threshold is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="name, condition_type et condition_threshold sont requis.")
    badge = Badge(**{k: v for k, v in payload.model_dump().items() if v is not None})
    db.add(badge)
    db.commit()
    db.refresh(badge)
    logger.info("Admin %s created badge id=%s name=%s", _admin.get("email"), badge.id, badge.name)
    return badge


@router.patch("/badges/{badge_id}")
def admin_update_badge(
    badge_id: UUID,
    payload: BadgeUpsertRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    from app.models.gamification import Badge
    badge = db.get(Badge, badge_id)
    if badge is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Badge introuvable.")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(badge, key, value)
    db.add(badge)
    db.commit()
    db.refresh(badge)
    logger.info("Admin %s updated badge id=%s", _admin.get("email"), badge.id)
    return badge


@router.delete("/badges/{badge_id}")
def admin_deactivate_badge(
    badge_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Soft-delete only — badges are permanently referenced by user_badges
    (unlock history must never be lost), so this deactivates rather than
    removes the row."""
    from app.models.gamification import Badge
    badge = db.get(Badge, badge_id)
    if badge is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Badge introuvable.")
    badge.active = False
    db.add(badge)
    db.commit()
    logger.info("Admin %s deactivated badge id=%s", _admin.get("email"), badge.id)
    return {"id": str(badge.id), "active": False}


class CosmeticUpsertRequest(BaseModel):
    type: Optional[str] = None
    code: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    asset_url: Optional[str] = None
    category: Optional[str] = None
    rarity: Optional[str] = None
    is_hidden: Optional[bool] = None
    season_id: Optional[UUID] = None
    event_key: Optional[str] = None
    available_from: Optional[datetime] = None
    available_until: Optional[datetime] = None
    is_active: Optional[bool] = None


@router.get("/cosmetics")
def admin_list_cosmetics(
    type: Optional[str] = Query(default=None),  # noqa: A002
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    from app.models.arena_cosmetics import ArenaCosmetic
    query = select(ArenaCosmetic)
    if type:
        query = query.where(ArenaCosmetic.type == type)
    rows = list(db.exec(query.order_by(ArenaCosmetic.type.asc(), ArenaCosmetic.name.asc())).all())
    return {"items": rows}


@router.post("/cosmetics")
def admin_create_cosmetic(
    payload: CosmeticUpsertRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    from app.models.arena_cosmetics import COSMETIC_TYPES, ArenaCosmetic
    if not payload.type or not payload.code or not payload.name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="type, code et name sont requis.")
    if payload.type not in COSMETIC_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="type invalide.")
    cosmetic = ArenaCosmetic(**{k: v for k, v in payload.model_dump().items() if v is not None})
    db.add(cosmetic)
    db.commit()
    db.refresh(cosmetic)
    logger.info("Admin %s created cosmetic id=%s code=%s", _admin.get("email"), cosmetic.id, cosmetic.code)
    return cosmetic


@router.patch("/cosmetics/{cosmetic_id}")
def admin_update_cosmetic(
    cosmetic_id: UUID,
    payload: CosmeticUpsertRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    from app.models.arena_cosmetics import ArenaCosmetic
    cosmetic = db.get(ArenaCosmetic, cosmetic_id)
    if cosmetic is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cosmétique introuvable.")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(cosmetic, key, value)
    db.add(cosmetic)
    db.commit()
    db.refresh(cosmetic)
    logger.info("Admin %s updated cosmetic id=%s", _admin.get("email"), cosmetic.id)
    return cosmetic


@router.delete("/cosmetics/{cosmetic_id}")
def admin_deactivate_cosmetic(
    cosmetic_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Soft-delete — inventory rows already granted to players must remain
    valid, so this deactivates from the catalogue rather than deleting."""
    from app.models.arena_cosmetics import ArenaCosmetic
    cosmetic = db.get(ArenaCosmetic, cosmetic_id)
    if cosmetic is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cosmétique introuvable.")
    cosmetic.is_active = False
    db.add(cosmetic)
    db.commit()
    logger.info("Admin %s deactivated cosmetic id=%s", _admin.get("email"), cosmetic.id)
    return {"id": str(cosmetic.id), "is_active": False}


class CollectionUpsertRequest(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    badge_ids: Optional[List[str]] = None
    reward_config: Optional[List[Dict[str, Any]]] = None
    is_active: Optional[bool] = None


@router.get("/collections")
def admin_list_collections(
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    from app.models.arena_cosmetics import ArenaCollection
    rows = list(db.exec(select(ArenaCollection).order_by(ArenaCollection.name.asc())).all())
    return {"items": rows}


@router.post("/collections")
def admin_create_collection(
    payload: CollectionUpsertRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    from app.models.arena_cosmetics import ArenaCollection
    if not payload.code or not payload.name or not payload.badge_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="code, name et badge_ids sont requis.")
    collection = ArenaCollection(**{k: v for k, v in payload.model_dump().items() if v is not None})
    db.add(collection)
    db.commit()
    db.refresh(collection)
    logger.info("Admin %s created collection id=%s code=%s", _admin.get("email"), collection.id, collection.code)
    return collection


@router.patch("/collections/{collection_id}")
def admin_update_collection(
    collection_id: UUID,
    payload: CollectionUpsertRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    from app.models.arena_cosmetics import ArenaCollection
    collection = db.get(ArenaCollection, collection_id)
    if collection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection introuvable.")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(collection, key, value)
    db.add(collection)
    db.commit()
    db.refresh(collection)
    logger.info("Admin %s updated collection id=%s", _admin.get("email"), collection.id)
    return collection


@router.delete("/collections/{collection_id}")
def admin_deactivate_collection(
    collection_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    from app.models.arena_cosmetics import ArenaCollection
    collection = db.get(ArenaCollection, collection_id)
    if collection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection introuvable.")
    collection.is_active = False
    db.add(collection)
    db.commit()
    logger.info("Admin %s deactivated collection id=%s", _admin.get("email"), collection.id)
    return {"id": str(collection.id), "is_active": False}


# ─── Phase 15 — LiveOps admin panel: Missions ───────────────────────────────
# Spec: "Create/Edit/Delete/Pause/Publish/Duplicate/Archive mission."

class MissionUpsertRequest(BaseModel):
    code: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    category: Optional[str] = None
    mission_type: Optional[str] = None
    period: Optional[str] = None
    difficulty: Optional[str] = None
    metric_key: Optional[str] = None
    target_value: Optional[int] = None
    time_window: Optional[Dict[str, Any]] = None
    reward_config: Optional[List[Dict[str, Any]]] = None
    season_id: Optional[UUID] = None
    event_id: Optional[UUID] = None
    max_free_rerolls: Optional[int] = None
    sort_order: Optional[int] = None
    status: Optional[str] = None


@router.get("/missions")
def admin_list_missions(
    period: Optional[str] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    from app.models.liveops import ArenaMission
    query = select(ArenaMission)
    if period:
        query = query.where(ArenaMission.period == period)
    if status_filter:
        query = query.where(ArenaMission.status == status_filter)
    rows = list(db.exec(query.order_by(ArenaMission.period.asc(), ArenaMission.sort_order.asc())).all())
    return {"items": rows}


@router.post("/missions")
def admin_create_mission(
    payload: MissionUpsertRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    from app.models.liveops import MISSION_DIFFICULTIES, MISSION_PERIODS, MISSION_TYPES, ArenaMission
    if not payload.code or not payload.title or not payload.mission_type or not payload.period or not payload.metric_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="code, title, mission_type, period et metric_key sont requis.")
    if payload.mission_type not in MISSION_TYPES or payload.period not in MISSION_PERIODS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="mission_type ou period invalide.")
    if payload.difficulty and payload.difficulty not in MISSION_DIFFICULTIES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="difficulty invalide.")
    mission = ArenaMission(created_by=UUID(_admin["id"]), **{k: v for k, v in payload.model_dump().items() if v is not None})
    db.add(mission)
    db.commit()
    db.refresh(mission)
    logger.info("Admin %s created mission id=%s code=%s", _admin.get("email"), mission.id, mission.code)
    return mission


@router.patch("/missions/{mission_id}")
def admin_update_mission(
    mission_id: UUID,
    payload: MissionUpsertRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    from app.models.liveops import ArenaMission
    mission = db.get(ArenaMission, mission_id)
    if mission is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mission introuvable.")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(mission, key, value)
    mission.updated_at = datetime.now(timezone.utc)
    db.add(mission)
    db.commit()
    db.refresh(mission)
    logger.info("Admin %s updated mission id=%s", _admin.get("email"), mission.id)
    return mission


def _set_mission_status(db: Session, mission_id: UUID, new_status: str, *, admin_email: Optional[str]):
    from app.models.liveops import ArenaMission
    mission = db.get(ArenaMission, mission_id)
    if mission is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mission introuvable.")
    mission.status = new_status
    mission.updated_at = datetime.now(timezone.utc)
    db.add(mission)
    db.commit()
    db.refresh(mission)
    logger.info("Admin %s set mission id=%s status=%s", admin_email, mission.id, new_status)
    return mission


@router.post("/missions/{mission_id}/publish")
def admin_publish_mission(mission_id: UUID, _admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db)):
    return _set_mission_status(db, mission_id, "published", admin_email=_admin.get("email"))


@router.post("/missions/{mission_id}/pause")
def admin_pause_mission(mission_id: UUID, _admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db)):
    return _set_mission_status(db, mission_id, "paused", admin_email=_admin.get("email"))


@router.post("/missions/{mission_id}/archive")
def admin_archive_mission(mission_id: UUID, _admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db)):
    return _set_mission_status(db, mission_id, "archived", admin_email=_admin.get("email"))


@router.post("/missions/{mission_id}/duplicate")
def admin_duplicate_mission(mission_id: UUID, _admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db)):
    from app.models.liveops import ArenaMission
    original = db.get(ArenaMission, mission_id)
    if original is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mission introuvable.")
    copy = ArenaMission(
        code=f"{original.code}_copy_{int(datetime.now(timezone.utc).timestamp())}",
        title=f"{original.title} (copie)", description=original.description, icon=original.icon,
        category=original.category, mission_type=original.mission_type, period=original.period,
        difficulty=original.difficulty, metric_key=original.metric_key, target_value=original.target_value,
        time_window=original.time_window, reward_config=original.reward_config, season_id=original.season_id,
        event_id=original.event_id, max_free_rerolls=original.max_free_rerolls, sort_order=original.sort_order,
        status="draft", created_by=UUID(_admin["id"]),
    )
    db.add(copy)
    db.commit()
    db.refresh(copy)
    logger.info("Admin %s duplicated mission id=%s -> %s", _admin.get("email"), mission_id, copy.id)
    return copy


@router.delete("/missions/{mission_id}")
def admin_delete_mission(mission_id: UUID, _admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db)):
    from app.models.liveops import ArenaMission, ArenaPlayerMission
    mission = db.get(ArenaMission, mission_id)
    if mission is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mission introuvable.")
    has_assignments = db.exec(select(ArenaPlayerMission).where(ArenaPlayerMission.mission_id == mission_id).limit(1)).first() is not None
    if has_assignments:
        mission.status = "archived"
        db.add(mission)
        db.commit()
        return {"id": str(mission_id), "status": "archived", "note": "Mission déjà assignée à des joueurs — archivée plutôt que supprimée."}
    db.delete(mission)
    db.commit()
    logger.info("Admin %s deleted mission id=%s", _admin.get("email"), mission_id)
    return {"id": str(mission_id), "status": "deleted"}


# ─── Phase 15 — LiveOps admin panel: Events ─────────────────────────────────
# Spec: "Create event / Schedule event / Cancel event / Monitor
# participation / Distribute rewards."

class EventUpsertRequest(BaseModel):
    event_type: Optional[str] = None
    code: Optional[str] = None
    name: Optional[str] = None
    banner_url: Optional[str] = None
    description: Optional[str] = None
    rules: Optional[str] = None
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    visibility: Optional[str] = None
    eligible_filter: Optional[Dict[str, Any]] = None
    config: Optional[Dict[str, Any]] = None
    reward_config: Optional[List[Dict[str, Any]]] = None


@router.get("/events")
def admin_list_events(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    event_type: Optional[str] = Query(default=None),
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    from app.services.competitive import event_service
    statuses = [status_filter] if status_filter else None
    events = event_service.list_events(db, statuses=statuses, event_type=event_type)
    return {"items": events}


@router.post("/events")
def admin_create_event(
    payload: EventUpsertRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    from app.services.competitive import event_service
    if not payload.code or not payload.name or not payload.event_type or not payload.starts_at or not payload.ends_at:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="code, name, event_type, starts_at et ends_at sont requis.")
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    event = event_service.create_event(db, created_by=UUID(_admin["id"]), **fields)
    return event


@router.patch("/events/{event_id}")
def admin_update_event(
    event_id: UUID,
    payload: EventUpsertRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    from app.services.competitive import event_service
    return event_service.update_event(db, event_id, **payload.model_dump(exclude_unset=True))


@router.post("/events/{event_id}/schedule")
def admin_schedule_event(event_id: UUID, _admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db)):
    from app.services.competitive import event_service
    event = event_service.schedule_event(db, event_id)
    logger.info("Admin %s scheduled event id=%s", _admin.get("email"), event_id)
    return event


@router.post("/events/{event_id}/cancel")
def admin_cancel_event(event_id: UUID, _admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db)):
    from app.services.competitive import event_service
    event = event_service.cancel_event(db, event_id)
    logger.info("Admin %s cancelled event id=%s", _admin.get("email"), event_id)
    return event


@router.post("/events/{event_id}/archive")
def admin_archive_event(event_id: UUID, _admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db)):
    from app.services.competitive import event_service
    return event_service.archive_event(db, event_id)


@router.get("/events/{event_id}/participation")
def admin_get_event_participation(event_id: UUID, _admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db)):
    from app.services.competitive import event_service
    return {"items": event_service.get_event_participation(db, event_id)}


@router.post("/events/{event_id}/distribute-rewards")
def admin_distribute_event_rewards(event_id: UUID, _admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db)):
    """Manually grants event.reward_config to every participant who hasn't
    claimed yet — the admin-triggered counterpart to the player's own
    POST /api/competitive/events/{id}/claim, for events whose reward is
    meant to be pushed rather than opted into."""
    from app.models.liveops import ArenaEventParticipant
    from app.services.competitive import event_service, reward_service
    event = event_service.get_event_or_404(db, event_id)
    participants = db.exec(
        select(ArenaEventParticipant).where(ArenaEventParticipant.event_id == event_id).where(ArenaEventParticipant.reward_claimed_at.is_(None))
    ).all()
    distributed = 0
    for participant in participants:
        for entry in (event.reward_config or []):
            reward_type = entry.get("reward_type")
            if not reward_type:
                continue
            reward_service.grant_reward(
                db, user_id=participant.user_id, season_id=None, source=f"event_admin_distribute:{event.id}",
                reward_type=reward_type, reward_ref=entry.get("reward_ref"), reward_amount=entry.get("reward_amount"),
                notify=True,
            )
        participant.reward_claimed_at = datetime.now(timezone.utc)
        db.add(participant)
        distributed += 1
    db.commit()
    logger.info("Admin %s distributed rewards for event id=%s to %s participants", _admin.get("email"), event_id, distributed)
    return {"event_id": str(event_id), "distributed": distributed}


# ─── Phase 15 — LiveOps analytics ───────────────────────────────────────────

@router.get("/liveops-analytics/missions")
def admin_liveops_mission_analytics(
    period: Optional[str] = Query(default=None),
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    from app.services.competitive import liveops_analytics_service
    return liveops_analytics_service.get_mission_analytics(db, period=period)


@router.get("/liveops-analytics/retention")
def admin_liveops_retention_analytics(_admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db)):
    from app.services.competitive import liveops_analytics_service
    return liveops_analytics_service.get_retention_proxies(db)


@router.get("/liveops-analytics/events")
def admin_liveops_event_analytics(_admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db)):
    from app.services.competitive import liveops_analytics_service
    return liveops_analytics_service.get_event_analytics(db)


@router.get("/liveops-analytics/rewards")
def admin_liveops_reward_analytics(_admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db)):
    from app.services.competitive import liveops_analytics_service
    return liveops_analytics_service.get_reward_distribution(db)


# ─── Phase 16 — Moderation: Reports, Sanctions, Appeals ─────────────────────

@router.get("/reports")
def admin_list_reports(
    report_status: Optional[str] = Query(default=None, alias="status"),
    category: Optional[str] = Query(default=None),
    target_type: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=100),
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    from app.services.competitive import moderation_service
    items, total = moderation_service.list_reports(db, report_status=report_status, category=category, target_type=target_type, page=page, size=size)
    return {"items": items, "total": total, "page": page, "size": size}


@router.get("/reports/{report_id}")
def admin_get_report(report_id: UUID, _admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db)):
    from app.services.competitive import moderation_service
    return moderation_service.get_report_or_404(db, report_id)


class ReportReviewRequest(BaseModel):
    new_status: str
    resolution_note: Optional[str] = None


@router.post("/reports/{report_id}/review")
def admin_review_report(
    report_id: UUID, payload: ReportReviewRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db),
):
    from app.services.competitive import moderation_service
    report = moderation_service.review_report(db, report_id, admin_id=UUID(_admin["id"]), new_status=payload.new_status, resolution_note=payload.resolution_note)
    logger.info("Admin %s reviewed report id=%s -> %s", _admin.get("email"), report_id, payload.new_status)
    return report


class ReportMergeRequest(BaseModel):
    into_report_id: UUID


@router.post("/reports/{report_id}/merge")
def admin_merge_report(
    report_id: UUID, payload: ReportMergeRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db),
):
    from app.services.competitive import moderation_service
    return moderation_service.merge_reports(db, report_id, into_report_id=payload.into_report_id, admin_id=UUID(_admin["id"]))


class SanctionIssueRequest(BaseModel):
    user_id: UUID
    sanction_type: str
    reason: Optional[str] = None
    evidence: List[Dict[str, Any]] = Field(default_factory=list)
    report_id: Optional[UUID] = None
    club_id: Optional[UUID] = None
    duration_days: Optional[int] = None


@router.post("/sanctions")
def admin_issue_sanction(
    payload: SanctionIssueRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db),
):
    from app.services.competitive import moderation_service
    sanction = moderation_service.issue_sanction(
        db, user_id=payload.user_id, sanction_type=payload.sanction_type, reason=payload.reason,
        issued_by=UUID(_admin["id"]), evidence=payload.evidence, report_id=payload.report_id,
        club_id=payload.club_id, duration_days=payload.duration_days,
    )
    logger.info("Admin %s issued sanction type=%s user=%s", _admin.get("email"), payload.sanction_type, payload.user_id)
    return sanction


@router.get("/sanctions")
def admin_list_sanctions(
    user_id: Optional[UUID] = Query(default=None),
    sanction_status: Optional[str] = Query(default=None, alias="status"),
    sanction_type: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=100),
    _admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db),
):
    from app.services.competitive import moderation_service
    items, total = moderation_service.list_sanctions(db, user_id=user_id, sanction_status=sanction_status, sanction_type=sanction_type, page=page, size=size)
    return {"items": items, "total": total, "page": page, "size": size}


class SanctionRevokeRequest(BaseModel):
    note: Optional[str] = None


@router.post("/sanctions/{sanction_id}/revoke")
def admin_revoke_sanction(
    sanction_id: UUID, payload: SanctionRevokeRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db),
):
    from app.services.competitive import moderation_service
    sanction = moderation_service.revoke_sanction(db, sanction_id, admin_id=UUID(_admin["id"]), note=payload.note)
    logger.info("Admin %s revoked sanction id=%s", _admin.get("email"), sanction_id)
    return sanction


@router.get("/appeals")
def admin_list_appeals(
    appeal_status: Optional[str] = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=100),
    _admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db),
):
    from app.services.competitive import moderation_service
    items, total = moderation_service.list_appeals(db, appeal_status=appeal_status, page=page, size=size)
    return {"items": items, "total": total, "page": page, "size": size}


class AppealReviewRequest(BaseModel):
    decision: str
    resolution_message: Optional[str] = None


@router.post("/appeals/{appeal_id}/review")
def admin_review_appeal(
    appeal_id: UUID, payload: AppealReviewRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db),
):
    from app.services.competitive import moderation_service
    appeal = moderation_service.review_appeal(db, appeal_id, admin_id=UUID(_admin["id"]), decision=payload.decision, resolution_message=payload.resolution_message)
    logger.info("Admin %s reviewed appeal id=%s -> %s", _admin.get("email"), appeal_id, payload.decision)
    return appeal


# ─── Phase 16 — Feature Flags ────────────────────────────────────────────────

@router.get("/feature-flags")
def admin_list_feature_flags(_admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db)):
    from app.services.competitive import feature_flags_service
    return feature_flags_service.list_flags(db)


class FeatureFlagUpdateRequest(BaseModel):
    enabled: bool


@router.patch("/feature-flags/{flag_name}")
def admin_set_feature_flag(
    flag_name: str, payload: FeatureFlagUpdateRequest,
    _admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db),
):
    from app.services.competitive import feature_flags_service
    flags = feature_flags_service.set_flag(db, flag_name, payload.enabled)
    logger.info("Admin %s set feature flag %s=%s", _admin.get("email"), flag_name, payload.enabled)
    return flags


# ─── Phase 16 — Monitoring: Metrics ──────────────────────────────────────────

@router.get("/metrics/overview")
def admin_metrics_overview(
    since_hours: int = Query(default=24, ge=1, le=720),
    _admin: Dict[str, Any] = Depends(get_admin_user), db: Session = Depends(get_db),
):
    from app.services.competitive import metrics_service
    return {
        "matches": metrics_service.get_match_metrics(db, since_hours=since_hours),
        "queue": metrics_service.get_queue_metrics(db, since_hours=since_hours),
        "answer_time": metrics_service.get_answer_time_metrics(db, since_hours=since_hours),
        "participation": metrics_service.get_participation_metrics(db, since_hours=since_hours),
        "realtime": metrics_service.get_realtime_snapshot(db),
    }
