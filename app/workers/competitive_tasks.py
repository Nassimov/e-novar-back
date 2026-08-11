from __future__ import annotations

"""
Competitive Arena — background tasks (Phase 1 expiry + Phase 2 reminders/
disconnect handling).

Expires stale pending invitations (and returns their match to 'draft' so
the creator can invite someone else) and stale draft matches that were
never followed by an invitation.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task
def task_expire_competitive_invitations() -> Dict[str, int]:
    from sqlmodel import Session, select

    from app.database import get_engine
    from app.models.competitive import CompetitiveInvitation, CompetitiveMatch
    from app.services.competitive import event_log_service, match_service
    from app.services.notification_engine import emit

    expired_invitations = 0
    expired_matches = 0
    now = datetime.now(timezone.utc)
    engine = get_engine()

    with Session(engine) as db:
        stale_invitations = db.exec(
            select(CompetitiveInvitation)
            .where(CompetitiveInvitation.status == "pending")
            .where(CompetitiveInvitation.expires_at <= now)
        ).all()
        for invitation in stale_invitations:
            invitation.status = "expired"
            invitation.responded_at = now
            db.add(invitation)
            match = db.get(CompetitiveMatch, invitation.match_id)
            if match is not None and match.status == "waiting_for_opponent":
                match_service.transition(match, "draft")
                db.add(match)
            db.commit()

            event_log_service.log_event(
                db, match_id=invitation.match_id, actor_id=None, event_type="invitation_expired",
                meta={"invitation_id": str(invitation.id)},
            )
            for uid in (invitation.inviter_id, invitation.invitee_id):
                emit(
                    db, event_type="competitive_invitation_expired", user_id=uid,
                    data={"match_id": str(invitation.match_id)},
                    dedup_key=f"competitive_invitation_expired:{invitation.id}:{uid}",
                )
            expired_invitations += 1

        stale_matches = db.exec(
            select(CompetitiveMatch)
            .where(CompetitiveMatch.status == "draft")
            .where(CompetitiveMatch.expires_at.is_not(None))
            .where(CompetitiveMatch.expires_at <= now)
        ).all()
        for match in stale_matches:
            match_service.transition(match, "expired")
            db.add(match)
            expired_matches += 1
        db.commit()

    logger.info(
        "task_expire_competitive_invitations: expired_invitations=%s expired_matches=%s",
        expired_invitations, expired_matches,
    )
    return {"expired_invitations": expired_invitations, "expired_matches": expired_matches}


@celery_app.task
def task_check_waiting_room_disconnect(match_id: str, user_id: str) -> Dict[str, str]:
    """Fired once (countdown=grace period) when a player disconnects from a
    match's Waiting Room WS. If they still haven't reconnected by the time
    this runs, apply the admin-configured policy (cancel the match, or mark
    the disconnector's forfeit) — a no-op if they reconnected, or if the
    match already moved on for any other reason."""
    from uuid import UUID

    from sqlmodel import Session

    from app.database import get_engine
    from app.models.admin import PlatformSettings
    from app.models.competitive import CompetitiveMatch, CompetitiveMatchParticipant
    from app.services.competitive import event_log_service, match_service, presence_service
    from app.services.notification_engine import emit

    engine = get_engine()
    with Session(engine) as db:
        match = db.get(CompetitiveMatch, UUID(match_id))
        if match is None or match.status not in ("waiting_room", "countdown"):
            return {"outcome": "noop_match_state"}

        presence = presence_service.get_presence(match.id)
        entry = presence.get(user_id)
        if entry and entry.get("status") != presence_service.STATUS_DISCONNECTED:
            return {"outcome": "reconnected"}

        settings_row = db.get(PlatformSettings, True) or PlatformSettings()
        participants = match_service.get_participants(db, match.id)
        other = next((p for p in participants if str(p.user_id) != user_id), None)

        if settings_row.competitive_disconnect_policy == "forfeit":
            disconnected = next((p for p in participants if str(p.user_id) == user_id), None)
            if disconnected:
                disconnected.result = "loss"
                db.add(disconnected)
            if other:
                other.result = "win"
                db.add(other)
            match_service.transition(match, "abandoned")
            outcome = "forfeited"
        else:
            match_service.transition(match, "cancelled")
            match.cancelled_reason = "opponent_disconnected"
            match.cancelled_at = match.updated_at
            outcome = "cancelled"
        db.add(match)
        db.commit()

        event_log_service.log_event(
            db, match_id=match.id, actor_id=None, event_type=f"disconnect_{outcome}",
            meta={"disconnected_user_id": user_id},
        )

        if other:
            emit(
                db, event_type="competitive_match_forfeited", user_id=other.user_id,
                dedup_key=f"competitive_match_forfeited:{match.id}",
            )
        return {"outcome": outcome}


@celery_app.task
def task_send_competitive_match_reminders() -> Dict[str, int]:
    """Runs every 5 min (see beat_schedule). For each admin-configured
    reminder window (default 24h/1h/15min before), notifies both
    participants of a scheduled match whose start now falls inside that
    window. Dedup via notification_engine.emit()'s dedup_key means a match
    lingering across two consecutive polls never double-notifies for the
    same window."""
    from datetime import timedelta

    from sqlmodel import Session, select

    from app.database import get_engine
    from app.models.admin import PlatformSettings
    from app.models.competitive import CompetitiveMatch
    from app.services.competitive import match_service

    sent = 0
    now = datetime.now(timezone.utc)
    engine = get_engine()

    with Session(engine) as db:
        settings_row = db.get(PlatformSettings, True) or PlatformSettings()
        poll_interval_minutes = 5

        for window_minutes in settings_row.competitive_reminder_minutes_before:
            window_start = now + timedelta(minutes=window_minutes)
            window_end = window_start + timedelta(minutes=poll_interval_minutes)
            matches = db.exec(
                select(CompetitiveMatch)
                .where(CompetitiveMatch.status == "scheduled")
                .where(CompetitiveMatch.scheduled_at >= window_start)
                .where(CompetitiveMatch.scheduled_at < window_end)
            ).all()
            for match in matches:
                for participant in match_service.get_participants(db, match.id):
                    result = emit(
                        db, event_type="competitive_match_reminder", user_id=participant.user_id,
                        context={"match_type": match.match_type},
                        data={"match_id": str(match.id)},
                        dedup_key=f"competitive_match_reminder:{match.id}:{window_minutes}",
                    )
                    if result is not None:
                        sent += 1

    logger.info("task_send_competitive_match_reminders: sent=%s", sent)
    return {"sent": sent}


@celery_app.task
def task_begin_match_after_countdown(match_id: str) -> Dict[str, str]:
    """Fired once, countdown=competitive_match_countdown_seconds after a
    match enters 'countdown' (both players readied up) — see
    app/routers/competitive/lobby.py's set_ready. No-ops if the match moved
    on for any other reason (e.g. an admin cancelled it) in the meantime."""
    from uuid import UUID

    from sqlmodel import Session

    from app.core.competitive_ws import publish_competitive_event
    from app.core.spectator_ws import publish_spectator_event
    from app.database import get_engine
    from app.models.competitive import CompetitiveMatch
    from app.services.competitive import gameplay_service

    engine = get_engine()
    with Session(engine) as db:
        match = db.get(CompetitiveMatch, UUID(match_id))
        if match is None or match.status != "countdown":
            return {"outcome": "noop"}
        gameplay_service.start_match(db, match)
        publish_competitive_event(match_id, {"type": "gameplay_update", "match_status": match.status})
        # Phase 8 — keep spectators in lockstep with players on every phase
        # transition (see app/core/spectator_ws.py + gameplay_service.
        # compute_spectator_state).
        publish_spectator_event(match_id, {"type": "spectator_update"})
        return {"outcome": "started"}


@celery_app.task
def task_gameplay_tick(match_id: str) -> Dict[str, str]:
    """The gameplay engine's heartbeat — fires exactly at the current
    question's next phase deadline (see gameplay_service.schedule_next_tick).
    Self-healing: calls ensure_progressed() which is a no-op if the match
    already advanced past this point (e.g. both players answered early and
    submit_answer already rescheduled a fresher tick)."""
    from uuid import UUID

    from sqlmodel import Session

    from app.core.competitive_ws import publish_competitive_event
    from app.core.spectator_ws import publish_spectator_event
    from app.database import get_engine
    from app.models.competitive import CompetitiveMatch
    from app.services.competitive import gameplay_service

    engine = get_engine()
    with Session(engine) as db:
        match = db.get(CompetitiveMatch, UUID(match_id))
        if match is None or match.status != "in_progress":
            return {"outcome": "noop"}
        gameplay_service.ensure_progressed(db, match)
        publish_competitive_event(match_id, {"type": "gameplay_update", "match_status": match.status})
        publish_spectator_event(match_id, {"type": "spectator_update"})  # Phase 8
        gameplay_service.schedule_next_tick(db, match)
        return {"outcome": "ticked", "status": match.status}


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def task_process_match_completion(self, match_id: str) -> Dict[str, str]:
    """Phase 4 — fired (fire-and-forget) from gameplay_service.finish_match().
    The match result is already visible to players the instant finish_match
    returns; this task builds the replay, the AI educational report,
    recommendations, roadmap, long-term stat breakdowns, and achievement
    hooks in the background, notifying the player once ready.

    Duplicate-job-safe: the compare-and-swap UPDATE below only proceeds if
    this is genuinely the first worker to pick up this match (handles
    Celery redelivery after a worker crash, or two near-simultaneous
    dispatches) — a second invocation is a clean no-op."""
    from uuid import UUID

    import sqlalchemy as sa
    from sqlmodel import Session, select

    from app.database import get_engine
    from app.models.competitive import (
        CompetitiveMatch,
        CompetitiveMatchEvent,
        CompetitiveMatchReplay,
        CompetitiveStatistics,
    )
    from app.services.competitive import (
        ai_report_service,
        analysis_service,
        event_log_service,
        match_service,
        recommendation_service,
        replay_service,
    )
    from app.services.notification_engine import emit

    engine = get_engine()
    with Session(engine) as db:
        match = db.get(CompetitiveMatch, UUID(match_id))
        if match is None or match.status != "completed":
            return {"outcome": "noop_not_completed"}

        replay = db.get(CompetitiveMatchReplay, match.id)
        if replay is None:
            replay = CompetitiveMatchReplay(match_id=match.id, status="pending")
            db.add(replay)
            try:
                db.commit()
            except Exception:
                db.rollback()  # another worker inserted it first — fine

        cas_result = db.exec(
            sa.update(CompetitiveMatchReplay)
            .where(CompetitiveMatchReplay.match_id == match.id)
            .where(CompetitiveMatchReplay.status == "pending")
            .values(status="processing")
        )
        db.commit()
        if cas_result.rowcount == 0:
            logger.info("task_process_match_completion: match=%s already processed/processing — no-op", match_id)
            return {"outcome": "already_processing_or_done"}

        try:
            replay_service.generate_replay(db, match)
            analysis_service.apply_mistake_categorization(db, match)

            participants = match_service.get_participants(db, match.id)
            replay_row = db.get(CompetitiveMatchReplay, match.id)
            score_timeline = replay_row.score_timeline if replay_row else []

            # Phase 8 — a spectator who bookmarked this match (and has since
            # left) gets notified through the normal notification engine
            # (NOT the live WS, which they're no longer connected to) once
            # the replay is actually ready. Participants already get
            # 'competitive_replay_ready' below — skip anyone who's also a
            # participant to avoid a duplicate notification.
            try:
                from app.models.competitive import CompetitiveMatchBookmark
                participant_ids = {p.user_id for p in participants}
                bookmarks = db.exec(
                    select(CompetitiveMatchBookmark).where(CompetitiveMatchBookmark.match_id == match.id)
                ).all()
                for bookmark in bookmarks:
                    if bookmark.user_id in participant_ids:
                        continue
                    emit(
                        db, event_type="competitive_bookmarked_replay_ready", user_id=bookmark.user_id,
                        data={"match_id": str(match.id)},
                        dedup_key=f"competitive_bookmarked_replay_ready:{match.id}:{bookmark.user_id}",
                    )
            except Exception:
                logger.debug("Phase 8 bookmark replay-ready notify failed for match=%s", match.id, exc_info=True)

            for participant in participants:
                uid = participant.user_id

                subject_breakdown, difficulty_breakdown = analysis_service.compute_breakdowns(db, uid)
                life_stats = db.get(CompetitiveStatistics, uid)
                if life_stats is not None:
                    life_stats.subject_breakdown = subject_breakdown
                    life_stats.difficulty_breakdown = difficulty_breakdown
                    db.add(life_stats)
                    db.commit()

                ai_report_service.generate_and_store_analysis(db, match, uid)

                chapter_weakness = analysis_service.compute_chapter_weakness(db, uid)
                from app.models.catalog import Subject as _Subject
                subject_names = {str(s.id): s.name for s in db.exec(select(_Subject)).all()}
                strengths, weaknesses = analysis_service.detect_weaknesses_and_strengths(
                    subject_breakdown, difficulty_breakdown, chapter_weakness, subject_names,
                )
                recommendation_service.generate_recommendations(
                    db, match, uid, weaknesses=weaknesses, subject_breakdown=subject_breakdown,
                )
                weakest_id = min(
                    (sid for sid, s in subject_breakdown.items() if s["attempts"] >= 2),
                    key=lambda sid: subject_breakdown[sid]["accuracy_pct"], default=None,
                )
                recommendation_service.generate_roadmap(
                    db, match, uid, weaknesses=weaknesses,
                    weakest_subject_name=subject_names.get(weakest_id) if weakest_id else None,
                )

                from app.models.competitive import CompetitiveMatchPlayerStats
                player_stats = db.get(CompetitiveMatchPlayerStats, (match.id, uid))
                if player_stats is not None and life_stats is not None:
                    fast_correct_ratio = analysis_service.compute_fast_correct_ratio(db, match, uid)
                    events = analysis_service.detect_achievements(
                        player_result=player_stats.result, accuracy_pct=player_stats.accuracy_pct,
                        best_streak=player_stats.best_streak, current_lifetime_streak=life_stats.current_streak,
                        fast_correct_ratio=fast_correct_ratio, score_timeline=score_timeline, user_id=str(uid),
                    )
                    for event_type in events:
                        event_log_service.log_event(db, match_id=match.id, actor_id=uid, event_type=event_type, meta={})

                emit(db, event_type="competitive_replay_ready", user_id=uid, dedup_key=f"competitive_replay_ready:{match.id}:{uid}")
                emit(db, event_type="competitive_ai_analysis_ready", user_id=uid, dedup_key=f"competitive_ai_analysis_ready:{match.id}:{uid}")
                emit(db, event_type="competitive_progress_updated", user_id=uid, dedup_key=f"competitive_progress_updated:{match.id}:{uid}")
                # Phase 10 Part C — Battle Royale replay-ready notification,
                # reusing this exact same async replay-generation-complete
                # hook every other match type's 'competitive_replay_ready'
                # already fires from, just above.
                if match.match_type == "battle_royale":
                    emit(db, event_type="battle_royale_replay_available", user_id=uid, dedup_key=f"battle_royale_replay_available:{match.id}:{uid}")

            event_log_service.log_event(db, match_id=match.id, actor_id=None, event_type="replay_and_analysis_completed", meta={})
            return {"outcome": "done"}
        except Exception as exc:
            db.rollback()
            replay = db.get(CompetitiveMatchReplay, match.id)
            if replay is not None:
                replay.status = "failed"
                replay.error = str(exc)[:500]
                db.add(replay)
                db.commit()
            logger.exception("task_process_match_completion failed for match=%s", match_id)
            raise self.retry(exc=exc)


@celery_app.task
def task_check_ingame_disconnect(match_id: str, user_id: str) -> Dict[str, str]:
    """Phase 5 — fired once (countdown=competitive_ingame_disconnect_grace_
    seconds) when a player disconnects from a match's WS DURING gameplay
    (status='in_progress'). Distinct from Phase 2's much longer waiting-room
    grace — a mid-question disconnect can't wait minutes. No-ops if they
    reconnected or the match moved on for any other reason."""
    from uuid import UUID

    from sqlmodel import Session

    from app.core.competitive_ws import publish_competitive_event
    from app.core.spectator_ws import publish_spectator_event
    from app.database import get_engine
    from app.models.competitive import CompetitiveMatch
    from app.services.competitive import connection_service, gameplay_service, presence_service

    engine = get_engine()
    with Session(engine) as db:
        match = db.get(CompetitiveMatch, UUID(match_id))
        if match is None or match.status != "in_progress":
            return {"outcome": "noop_match_state"}

        presence = presence_service.get_presence(match.id)
        entry = presence.get(user_id)
        if entry and entry.get("status") != presence_service.STATUS_DISCONNECTED:
            return {"outcome": "reconnected"}

        result = gameplay_service.forfeit_by_disconnect(db, match, disconnected_user_id=UUID(user_id))
        if result is None:
            return {"outcome": "noop"}

        connection_service.log_connection_event(db, match_id=match.id, user_id=UUID(user_id), event_type="disconnected")
        publish_competitive_event(match_id, {"type": "gameplay_update", "match_status": result.status})
        publish_spectator_event(match_id, {"type": "spectator_update"})  # Phase 8
        return {"outcome": "forfeited"}


@celery_app.task
def task_check_battle_royale_disconnect(match_id: str, user_id: str) -> Dict[str, str]:
    """Phase 10, Part B — the Battle Royale-specific counterpart of
    task_check_ingame_disconnect (deliberately NOT reused/modified: that
    task forfeits the ENTIRE match to "the other player", which only makes
    sense for a 2-player duel — an N-player Battle Royale must never end
    just because one of up to 100 players disconnected). Fired once
    (countdown=CompetitiveBattleRoyaleMatch.disconnect_grace_seconds) when a
    BR player's WS disconnects DURING gameplay (see app/main.py's
    websocket_competitive disconnect branch). No-ops if they reconnected,
    the match/BR config moved on, or they were already eliminated/left by
    the time this fires.

    disconnect_policy handling:
      - 'auto_eliminate': eliminate them right now, with the exact same
        bookkeeping (eliminated_at/elimination_round/provisional final_rank/
        WS signal/remaining_players_count/phase cascade) as a normal
        per-question elimination — see battle_royale_service.
        eliminate_participant_for_disconnect.
      - 'reconnect_grace': intentionally a no-op here. The player simply
        misses questions until they reconnect (or a normal elimination
        mechanic catches up with them: under 'lives'/'survival', silence
        already reads as a wrong answer every question; under
        'score_elimination', missing answers only costs them relative
        score, so 'reconnect_grace' is mostly meaningful for that mode)."""
    from uuid import UUID

    from sqlmodel import Session, select

    from app.core.competitive_ws import publish_competitive_event
    from app.core.spectator_ws import publish_spectator_event
    from app.database import get_engine
    from app.models.competitive import CompetitiveBattleRoyaleMatch, CompetitiveMatch, CompetitiveMatchParticipant
    from app.services.competitive import battle_royale_service, connection_service, presence_service

    engine = get_engine()
    with Session(engine) as db:
        match = db.get(CompetitiveMatch, UUID(match_id))
        if match is None or match.status != "in_progress" or match.match_type != "battle_royale":
            return {"outcome": "noop_match_state"}
        br = db.get(CompetitiveBattleRoyaleMatch, match.id)
        if br is None or br.current_phase == "completed":
            return {"outcome": "noop_br_completed"}

        presence = presence_service.get_presence(match.id)
        entry = presence.get(user_id)
        if entry and entry.get("status") != presence_service.STATUS_DISCONNECTED:
            return {"outcome": "reconnected"}

        participant = db.exec(
            select(CompetitiveMatchParticipant)
            .where(CompetitiveMatchParticipant.match_id == match.id)
            .where(CompetitiveMatchParticipant.user_id == UUID(user_id))
        ).first()
        if participant is None or participant.left_at is not None or participant.elimination_round is not None:
            return {"outcome": "noop_not_active_participant"}

        connection_service.log_connection_event(db, match_id=match.id, user_id=UUID(user_id), event_type="disconnected")

        if br.disconnect_policy == "auto_eliminate":
            battle_royale_service.eliminate_participant_for_disconnect(db, match, br, participant)
            outcome = "auto_eliminated"
        else:
            outcome = "grace_noop"

        publish_competitive_event(match_id, {"type": "gameplay_update"})
        publish_spectator_event(match_id, {"type": "spectator_update"})  # Phase 8
        return {"outcome": outcome}


@celery_app.task
def task_detect_stale_gameplay_connections() -> Dict[str, int]:
    """Phase 5 Heartbeat Manager — periodic sweep (see beat_schedule) that
    catches SILENT disconnects (frozen tab, killed process — no clean
    WebSocket close event ever fires) that the WS handler's own
    try/finally can't see. A clean disconnect is already handled instantly
    by the WS handler itself; this is the safety net for everything else."""
    from datetime import datetime, timezone

    from sqlmodel import Session, select

    from app.core.competitive_ws import publish_competitive_event
    from app.core.spectator_ws import publish_spectator_event
    from app.database import get_engine
    from app.models.admin import PlatformSettings
    from app.models.competitive import CompetitiveMatch
    from app.services.competitive import connection_service, match_service, presence_service

    engine = get_engine()
    flagged = 0
    with Session(engine) as db:
        settings_row = db.get(PlatformSettings, True) or PlatformSettings()
        timeout_seconds = settings_row.competitive_heartbeat_timeout_seconds
        grace_seconds = settings_row.competitive_ingame_disconnect_grace_seconds
        now = datetime.now(timezone.utc)

        matches = db.exec(select(CompetitiveMatch).where(CompetitiveMatch.status == "in_progress")).all()
        for match in matches:
            presence = presence_service.get_presence(match.id)
            for participant in match_service.get_participants(db, match.id):
                entry = presence.get(str(participant.user_id))
                if entry is None or entry.get("status") == presence_service.STATUS_DISCONNECTED:
                    continue
                last_seen_raw = entry.get("last_seen")
                if not last_seen_raw:
                    continue
                try:
                    last_seen = datetime.fromisoformat(last_seen_raw)
                except ValueError:
                    continue
                if (now - last_seen).total_seconds() <= timeout_seconds:
                    continue

                presence_service.set_status(match.id, participant.user_id, presence_service.STATUS_DISCONNECTED)
                connection_service.log_connection_event(
                    db, match_id=match.id, user_id=participant.user_id, event_type="heartbeat_timeout",
                )
                publish_competitive_event(str(match.id), {"type": "gameplay_update", "match_status": match.status})
                publish_spectator_event(str(match.id), {"type": "spectator_update"})  # Phase 8
                try:
                    task_check_ingame_disconnect.apply_async(
                        args=[str(match.id), str(participant.user_id)], countdown=grace_seconds,
                    )
                except Exception:
                    logger.exception("failed to schedule ingame disconnect check for match=%s user=%s", match.id, participant.user_id)
                flagged += 1

    return {"flagged": flagged}


@celery_app.task
def task_check_competitive_season_end() -> Dict[str, Any]:
    """Phase 7 — Season End Workflow's Celery beat entry point (see
    beat_schedule). No-ops cleanly when there is no active season, or the
    active season's ends_at hasn't elapsed yet (both handled inside
    season_service.check_and_trigger_season_end)."""
    from sqlmodel import Session

    from app.database import get_engine
    from app.services.competitive import season_service

    engine = get_engine()
    with Session(engine) as db:
        ended_season_id = season_service.check_and_trigger_season_end(db)

    if ended_season_id is not None:
        logger.info("task_check_competitive_season_end: ended season=%s", ended_season_id)
        return {"ended_season_id": str(ended_season_id)}
    return {"ended_season_id": None}


@celery_app.task
def task_notify_competitive_season_ending_soon() -> Dict[str, int]:
    """Phase 7 — notifies every active-season participant once, when the
    season enters its configured 'ending soon' window (admin-configurable
    via PlatformSettings.competitive_season_ending_soon_hours). No-ops
    cleanly when there is no active season, or it's not inside that window
    yet (both handled inside season_service.check_and_notify_ending_soon)."""
    from sqlmodel import Session

    from app.database import get_engine
    from app.services.competitive import season_service

    engine = get_engine()
    with Session(engine) as db:
        sent = season_service.check_and_notify_ending_soon(db)

    return {"sent": sent}


@celery_app.task
def task_apply_competitive_inactivity_decay() -> Dict[str, int]:
    """Phase 13 — optional (PlatformSettings.competitive_inactivity_decay_
    enabled, default False — a genuine no-op until an admin opts in).
    Decays Arena Rating for players inactive past the configured threshold,
    at most once per week per player, floored — see ranking_service.
    apply_inactivity_decay's own docstring for the exact rules."""
    from sqlmodel import Session

    from app.database import get_engine
    from app.services.competitive import ranking_service

    engine = get_engine()
    with Session(engine) as db:
        decayed = ranking_service.apply_inactivity_decay(db)

    return {"decayed": decayed}


@celery_app.task
def task_flush_reaction_group(match_id: str, emoji: str) -> Dict[str, int]:
    """Phase 8 — spectator reaction grouping. Fires once,
    _REACTION_GROUP_WINDOW_SECONDS after the FIRST reaction of a (match,
    emoji) burst (see spectator_service.create_reaction) — reads however
    many accumulated in the Redis counter during that window and broadcasts
    ONE grouped `reaction` event, then clears the counter. Spec: "Grouped
    automatically. Do not flood the interface." No DB access needed — the
    reaction rows themselves were already persisted synchronously by the
    REST endpoint; this task only concerns the real-time broadcast."""
    from app.core.redis import get_redis_client
    from app.core.spectator_ws import publish_spectator_event
    from app.services.competitive.spectator_service import _reaction_group_key

    key = _reaction_group_key(match_id, emoji)
    try:
        r = get_redis_client()
        count = int(r.get(key) or 0)
        r.delete(key)
    except Exception:
        logger.debug("task_flush_reaction_group: redis unavailable match=%s emoji=%s", match_id, emoji, exc_info=True)
        count = 1

    if count > 0:
        publish_spectator_event(match_id, {"type": "reaction", "emoji": emoji, "count": count})
    return {"match_id": match_id, "emoji": emoji, "count": count}


@celery_app.task
def task_matchmaking_sweep() -> Dict[str, int]:
    """Phase 6 — the Matchmaking Engine's own heartbeat (see beat_schedule,
    every few seconds: minimizing wait time is the whole point). Pairs
    compatible 'searching' entries into proposals, then resolves any
    proposal whose accept-countdown has elapsed without both sides
    accepting. Both steps run every tick so a just-expired proposal's
    freed-up players can be immediately re-considered in the same pass."""
    from sqlmodel import Session

    from app.database import get_engine
    from app.services.competitive import matchmaking_service

    engine = get_engine()
    with Session(engine) as db:
        proposals_created = matchmaking_service.run_matchmaking_sweep(db)
        proposals_expired = matchmaking_service.expire_stale_proposals(db)

    return {"proposals_created": proposals_created, "proposals_expired": proposals_expired}


@celery_app.task
def task_battle_royale_autostart_check(match_id: str) -> Dict[str, str]:
    """Phase 10 — fired once (countdown=auto_start_countdown_seconds) after
    a Battle Royale's waiting room first reaches min_players (see
    app/services/competitive/battle_royale_service.py's
    _start_autostart_countdown). Re-checks the player count is still
    >= min_players before actually kicking off the real gameplay countdown
    (a player could have left during the wait) — falls back to
    'waiting_room' otherwise. No-ops if the match has already progressed
    for any other reason (early-full-lobby start, admin force-start,
    cancellation, ...) in the meantime."""
    from uuid import UUID

    from sqlmodel import Session

    from app.database import get_engine
    from app.models.competitive import CompetitiveBattleRoyaleMatch, CompetitiveMatch
    from app.services.competitive import battle_royale_service

    engine = get_engine()
    with Session(engine) as db:
        match = db.get(CompetitiveMatch, UUID(match_id))
        if match is None:
            return {"outcome": "noop_no_match"}
        br = db.get(CompetitiveBattleRoyaleMatch, match.id)
        if br is None or br.current_phase != "countdown" or match.status != "waiting_room":
            return {"outcome": "noop_already_progressed"}

        if (br.remaining_players_count or 0) < br.min_players:
            battle_royale_service.revert_autostart_countdown(db, match, br)
            return {"outcome": "reverted_below_min_players"}

        battle_royale_service.begin_gameplay_countdown(db, match, br)
        return {"outcome": "gameplay_countdown_started"}
