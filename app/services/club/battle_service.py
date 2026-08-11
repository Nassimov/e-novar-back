from __future__ import annotations

"""
Club Battle Coordinator — Competitive Arena Phase 11, Part C.

A Club Battle is NOT a new gameplay system — it IS a CompetitiveMatch row
(match_type='club_battle', reserved since Phase 1) with 2*team_size
participants split into two teams via the club_id/team_side columns added
to CompetitiveMatchParticipant (migration 091). Every piece of actual
gameplay (question serving/timing/WS push/anti-cheat/replay/spectator mode)
is reused UNCHANGED — this module only orchestrates the parts a generic
1v1/N-player match has no concept of: the club-to-club challenge handshake,
team roster validation, and what happens to the two CLUBS (not just the
individual players) once the match ends.

Ready-check is NOT reimplemented here — once a match is created (on
challenge accept), the EXISTING generic lobby endpoints (app/routers/
competitive/lobby.py's POST .../lobby/ready, already `all(p.is_ready for p
in participants)`-generic for any N) drive waiting_room -> countdown ->
gameplay_service.start_match verbatim. This module's only gameplay-adjacent
hook is finalize_club_battle(), called from gameplay_service.
ensure_progressed's exhausted-questions branch exactly the way
battle_royale_service.complete_from_exhausted_questions is.

Club-level rating is a flat, admin-configurable gain/loss (NOT Elo — see
PlatformSettings.competitive_club_rating_victory_gain/defeat_loss), with a
"protection" window (first N battles: halved loss) — deliberately simpler
than the player-level Ranking Engine's Elo, per spec's own "Victory gain /
Defeat loss / Season reset / Protection" wording (a fixed menu of knobs, not
a formula). Individual Arena Rating is never touched by a club battle (see
gameplay_service.finish_match's is_club_battle branch) — "Club Rating...
Independent from player rating" per spec.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.models.admin import PlatformSettings
from app.models.club import (
    CLUB_BATTLE_TEAM_SIZES,
    Club,
    ClubBattleChallenge,
    ClubBattleMatch,
    ClubMember,
    ClubRatingHistory,
    ClubSeason,
)
from app.models.competitive import CompetitiveMatch, CompetitiveMatchParticipant
from app.models.profile import Profile
from app.services.club import achievement_service, anti_abuse_service, feed_service, reputation_service
from app.services.club.permission_service import get_club_or_404, require_permission
from app.services.competitive import event_log_service, match_service
from app.services.notification_engine import emit

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _settings(db: Session) -> PlatformSettings:
    return db.get(PlatformSettings, True) or PlatformSettings()


# ─── Roster validation ────────────────────────────────────────────────────

def _validate_roster(db: Session, club_id: UUID, roster: List[UUID], team_size: int) -> None:
    if len(roster) != team_size:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"La composition doit contenir exactement {team_size} joueurs.")
    if len(set(roster)) != len(roster):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Un même joueur ne peut pas apparaître deux fois dans la composition.")

    active_ids = set(db.exec(
        select(ClubMember.user_id).where(ClubMember.club_id == club_id).where(ClubMember.status == "active")
    ).all())
    for uid in roster:
        if uid not in active_ids:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tous les joueurs sélectionnés doivent être membres actifs du club.")

    busy = db.exec(
        select(CompetitiveMatchParticipant.user_id)
        .join(CompetitiveMatch, CompetitiveMatch.id == CompetitiveMatchParticipant.match_id)
        .where(CompetitiveMatchParticipant.user_id.in_(roster))
        .where(CompetitiveMatch.match_type == "club_battle")
        .where(CompetitiveMatch.status.in_(("scheduled", "waiting_room", "countdown", "in_progress")))
    ).all()
    if busy:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Un ou plusieurs joueurs participent déjà à une autre bataille de club.")


# ─── Challenges ───────────────────────────────────────────────────────────

def create_challenge(
    db: Session, *, challenger_club_id: UUID, created_by: UUID, opponent_club_id: UUID,
    challenger_roster: List[UUID], team_size: Optional[int] = None, subject_ids: Optional[List[UUID]] = None,
    school_level_id: Optional[UUID] = None, difficulty: Optional[str] = None, question_count: int = 10,
    proposed_scheduled_at: Optional[datetime] = None, message: Optional[str] = None,
) -> ClubBattleChallenge:
    if challenger_club_id == opponent_club_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Un club ne peut pas se défier lui-même.")

    challenger = get_club_or_404(db, challenger_club_id)
    opponent = get_club_or_404(db, opponent_club_id)
    require_permission(db, challenger_club_id, created_by, "schedule_battles")
    if opponent.status != "active":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce club n'est pas disponible pour une bataille.")

    settings_row = _settings(db)
    resolved_team_size = team_size or settings_row.competitive_club_battle_team_size_default
    if resolved_team_size not in CLUB_BATTLE_TEAM_SIZES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Taille d'équipe invalide (options: {CLUB_BATTLE_TEAM_SIZES}).")

    existing_pending = db.exec(
        select(ClubBattleChallenge)
        .where(ClubBattleChallenge.challenger_club_id == challenger_club_id)
        .where(ClubBattleChallenge.opponent_club_id == opponent_club_id)
        .where(ClubBattleChallenge.status == "pending")
    ).first()
    if existing_pending is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Un défi est déjà en attente envers ce club.")

    _validate_roster(db, challenger_club_id, challenger_roster, resolved_team_size)

    challenge = ClubBattleChallenge(
        challenger_club_id=challenger_club_id, opponent_club_id=opponent_club_id, created_by=created_by,
        team_size=resolved_team_size, subject_ids=subject_ids or [], school_level_id=school_level_id,
        difficulty=difficulty, question_count=question_count, proposed_scheduled_at=proposed_scheduled_at,
        message=message, challenger_roster=challenger_roster,
        expires_at=_now() + timedelta(hours=settings_row.competitive_club_battle_challenge_expiry_hours),
    )
    db.add(challenge)
    db.commit()
    db.refresh(challenge)

    feed_service.record(db, club_id=challenger_club_id, event_type="club_battle_challenge_sent", actor_id=created_by, data={"opponent_club_id": str(opponent_club_id)})

    recipients = db.exec(
        select(ClubMember.user_id).where(ClubMember.club_id == opponent_club_id)
        .where(ClubMember.status == "active").where(ClubMember.role.in_(("owner", "officer")))
    ).all()
    for uid in recipients:
        emit(
            db, event_type="club_battle_challenge_received", user_id=uid,
            context={"club_name": challenger.name}, data={"challenge_id": str(challenge.id), "challenger_club_id": str(challenger_club_id)},
            dedup_key=f"club_battle_challenge_received:{challenge.id}:{uid}",
        )
    return challenge


def get_challenge_or_404(db: Session, challenge_id: UUID) -> ClubBattleChallenge:
    challenge = db.get(ClubBattleChallenge, challenge_id)
    if challenge is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Défi introuvable.")
    return challenge


def list_challenges(db: Session, club_id: UUID, *, direction: str = "incoming", status_filter: Optional[str] = "pending") -> List[ClubBattleChallenge]:
    query = select(ClubBattleChallenge)
    query = query.where(ClubBattleChallenge.opponent_club_id == club_id) if direction == "incoming" else query.where(ClubBattleChallenge.challenger_club_id == club_id)
    if status_filter:
        query = query.where(ClubBattleChallenge.status == status_filter)
    return list(db.exec(query.order_by(ClubBattleChallenge.created_at.desc())).all())


def cancel_challenge(db: Session, challenge: ClubBattleChallenge, actor_id: UUID) -> ClubBattleChallenge:
    require_permission(db, challenge.challenger_club_id, actor_id, "schedule_battles")
    if challenge.status != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce défi n'est plus en attente.")
    challenge.status = "cancelled"
    challenge.responded_at = _now()
    db.add(challenge)
    db.commit()
    db.refresh(challenge)
    return challenge


def respond_to_challenge(
    db: Session, challenge: ClubBattleChallenge, actor_id: UUID, *, accept: bool, opponent_roster: Optional[List[UUID]] = None,
) -> Dict[str, Any]:
    if challenge.status != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce défi n'est plus en attente.")
    require_permission(db, challenge.opponent_club_id, actor_id, "schedule_battles")

    challenger = get_club_or_404(db, challenge.challenger_club_id)
    opponent = get_club_or_404(db, challenge.opponent_club_id)

    if not accept:
        challenge.status = "declined"
        challenge.responded_at = _now()
        db.add(challenge)
        db.commit()
        emit(
            db, event_type="club_battle_challenge_declined", user_id=challenge.created_by,
            context={"club_name": opponent.name}, data={"challenge_id": str(challenge.id)},
            dedup_key=f"club_battle_challenge_declined:{challenge.id}",
        )
        return {"challenge": challenge, "match_id": None}

    if opponent_roster is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="La composition d'équipe est requise pour accepter.")
    _validate_roster(db, challenge.opponent_club_id, opponent_roster, challenge.team_size)
    # Challenger roster might have gone stale (a member left/got banned since
    # the challenge was sent) — re-validate right before match creation.
    _validate_roster(db, challenge.challenger_club_id, challenge.challenger_roster, challenge.team_size)

    match = _create_match_from_challenge(db, challenge, opponent_roster)
    challenge.status = "accepted"
    challenge.match_id = match.id
    challenge.responded_at = _now()
    db.add(challenge)
    db.commit()
    db.refresh(challenge)

    feed_service.record(db, club_id=challenger.id, event_type="club_battle_challenge_accepted", data={"opponent_club_id": str(opponent.id), "match_id": str(match.id)})
    feed_service.record(db, club_id=opponent.id, event_type="club_battle_challenge_accepted", data={"opponent_club_id": str(challenger.id), "match_id": str(match.id)})
    emit(
        db, event_type="club_battle_challenge_accepted", user_id=challenge.created_by,
        context={"club_name": opponent.name}, data={"challenge_id": str(challenge.id), "match_id": str(match.id)},
        dedup_key=f"club_battle_challenge_accepted:{challenge.id}",
    )
    return {"challenge": challenge, "match_id": match.id}


def _create_match_from_challenge(db: Session, challenge: ClubBattleChallenge, opponent_roster: List[UUID]) -> CompetitiveMatch:
    scheduled = challenge.proposed_scheduled_at
    match = CompetitiveMatch(
        match_type="club_battle", status="scheduled" if scheduled else "waiting_room",
        creator_id=None, subject_ids=challenge.subject_ids, school_level_id=challenge.school_level_id,
        difficulty=challenge.difficulty, question_count=challenge.question_count,
        # 'public' — a club battle is a community exhibition match, per
        # spec's "Reuse: Spectator Mode" (unlike a private 1v1 duel, both
        # clubs' members are expected to be able to watch their team play;
        # see app/routers/competitive/spectate.py's visibility=='public' gate).
        visibility="public",
        max_players=challenge.team_size * 2, scheduled_at=scheduled, created_by=challenge.created_by,
        # Phase 13 — semantically False: a club battle never touches
        # individual Arena Rating regardless of this flag (see
        # gameplay_service.finish_match's is_club_battle carve-out, which
        # predates and is independent of is_ranked) — set here only so
        # match history/filtering displays it accurately as non-ranked for
        # the INDIVIDUAL player (the club's own separate rating still moves).
        is_ranked=False,
    )
    db.add(match)
    db.commit()
    db.refresh(match)

    db.add(ClubBattleMatch(
        match_id=match.id, challenge_id=challenge.id, club_a_id=challenge.challenger_club_id,
        club_b_id=challenge.opponent_club_id, team_size=challenge.team_size,
    ))
    for uid in challenge.challenger_roster:
        db.add(CompetitiveMatchParticipant(match_id=match.id, user_id=uid, club_id=challenge.challenger_club_id, team_side="a"))
    for uid in opponent_roster:
        db.add(CompetitiveMatchParticipant(match_id=match.id, user_id=uid, club_id=challenge.opponent_club_id, team_side="b"))
    db.commit()

    event_log_service.log_event(db, match_id=match.id, actor_id=None, event_type="club_battle_created", meta={
        "club_a_id": str(challenge.challenger_club_id), "club_b_id": str(challenge.opponent_club_id), "team_size": challenge.team_size,
    })
    return match


# ─── Read ─────────────────────────────────────────────────────────────────

def _team_out(db: Session, club_id: UUID, participants: List[CompetitiveMatchParticipant]) -> Dict[str, Any]:
    club = db.get(Club, club_id)
    profiles = {p.id: p for p in db.exec(select(Profile).where(Profile.id.in_([pp.user_id for pp in participants]))).all()}
    return {
        "club_id": club_id, "club_name": club.name if club else None, "club_tag": club.tag if club else None,
        "score": sum(p.score for p in participants),
        "players": [
            {"user_id": p.user_id, "full_name": profiles[p.user_id].full_name if p.user_id in profiles else None, "is_ready": p.is_ready, "score": p.score}
            for p in participants
        ],
    }


def get_club_battle_state(db: Session, match: CompetitiveMatch) -> Dict[str, Any]:
    cb = db.get(ClubBattleMatch, match.id)
    if cb is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bataille de club introuvable.")
    participants = match_service.get_participants(db, match.id)
    team_a = [p for p in participants if p.team_side == "a"]
    team_b = [p for p in participants if p.team_side == "b"]
    return {
        "match_id": match.id, "status": match.status, "team_size": cb.team_size,
        "team_a": _team_out(db, cb.club_a_id, team_a), "team_b": _team_out(db, cb.club_b_id, team_b),
        "winner_club_id": cb.winner_club_id, "mvp_user_id": cb.mvp_user_id,
        "scheduled_at": match.scheduled_at, "started_at": match.started_at, "completed_at": match.completed_at,
    }


def list_club_battles(db: Session, club_id: UUID, *, upcoming_only: bool = False, limit: int = 20) -> List[Dict[str, Any]]:
    query = select(ClubBattleMatch).where((ClubBattleMatch.club_a_id == club_id) | (ClubBattleMatch.club_b_id == club_id))
    rows = list(db.exec(query.order_by(ClubBattleMatch.created_at.desc()).limit(limit)).all())
    out = []
    for cb in rows:
        match = db.get(CompetitiveMatch, cb.match_id)
        if match is None:
            continue
        if upcoming_only and match.status in ("completed", "cancelled", "expired", "abandoned", "disconnected"):
            continue
        opponent_id = cb.club_b_id if cb.club_a_id == club_id else cb.club_a_id
        opponent = db.get(Club, opponent_id)
        out.append({
            "match_id": match.id, "status": match.status, "opponent_club_id": opponent_id,
            "opponent_club_name": opponent.name if opponent else None, "team_size": cb.team_size,
            "own_score": cb.club_a_score if cb.club_a_id == club_id else cb.club_b_score,
            "opponent_score": cb.club_b_score if cb.club_a_id == club_id else cb.club_a_score,
            "won": cb.winner_club_id == club_id if cb.winner_club_id else None,
            "scheduled_at": match.scheduled_at, "completed_at": match.completed_at,
        })
    return out


# ─── Finalization (called from gameplay_service) ───────────────────────────

def finalize_club_battle(db: Session, match: CompetitiveMatch) -> CompetitiveMatch:
    from app.services.competitive import battle_royale_service, gameplay_service

    cb = db.get(ClubBattleMatch, match.id)
    if cb is None:
        logger.error("finalize_club_battle: no ClubBattleMatch companion row for match=%s — falling back to generic finish_match", match.id)
        return gameplay_service.finish_match(db, match)

    participants = match_service.get_participants(db, match.id)
    team_a = [p for p in participants if p.team_side == "a"]
    team_b = [p for p in participants if p.team_side == "b"]
    score_a = sum(p.score for p in team_a)
    score_b = sum(p.score for p in team_b)

    if score_a > score_b:
        winner_side, winner_club_id = "a", cb.club_a_id
    elif score_b > score_a:
        winner_side, winner_club_id = "b", cb.club_b_id
    else:
        winner_side, winner_club_id = None, None

    forced_results: Dict[UUID, str] = {}
    for p in team_a:
        forced_results[p.user_id] = "draw" if winner_side is None else ("win" if winner_side == "a" else "loss")
    for p in team_b:
        forced_results[p.user_id] = "draw" if winner_side is None else ("win" if winner_side == "b" else "loss")

    attempt_stats = battle_royale_service.bulk_attempt_stats(db, match.id)

    def _mvp_key(p: CompetitiveMatchParticipant):
        s = attempt_stats.get(p.user_id, {})
        return (-p.score, -(s.get("accuracy_pct") or 0), s.get("avg_response_time_ms") or 10**9)

    mvp = min(participants, key=_mvp_key) if participants else None

    cb.club_a_score = score_a
    cb.club_b_score = score_b
    cb.winner_club_id = winner_club_id
    cb.mvp_user_id = mvp.user_id if mvp else None
    cb.updated_at = _now()
    db.add(cb)
    db.commit()

    match = gameplay_service.finish_match(db, match, forced_results=forced_results)

    try:
        _apply_club_battle_results(db, match, cb, team_a=team_a, team_b=team_b, attempt_stats=attempt_stats)
    except Exception:
        logger.exception("club battle post-finish processing failed match=%s — match result itself is already committed", match.id)

    return match


def _is_perfect_team(attempt_stats: Dict[UUID, Dict[str, Any]], team: List[CompetitiveMatchParticipant]) -> bool:
    return bool(team) and all((attempt_stats.get(p.user_id, {}).get("accuracy_pct") == 100) for p in team)


def _apply_club_battle_results(
    db: Session, match: CompetitiveMatch, cb: ClubBattleMatch, *,
    team_a: List[CompetitiveMatchParticipant], team_b: List[CompetitiveMatchParticipant],
    attempt_stats: Dict[UUID, Dict[str, Any]],
) -> None:
    club_a = db.get(Club, cb.club_a_id)
    club_b = db.get(Club, cb.club_b_id)
    if club_a is None or club_b is None:
        return
    settings_row = _settings(db)
    season = db.exec(select(ClubSeason).where(ClubSeason.status == "active")).first()

    if cb.winner_club_id == club_a.id:
        result_a, result_b = "win", "loss"
    elif cb.winner_club_id == club_b.id:
        result_a, result_b = "loss", "win"
    else:
        result_a, result_b = "draw", "draw"

    _apply_club_rating(db, club_a, opponent=club_b, result=result_a, match=match, season=season, settings_row=settings_row)
    _apply_club_rating(db, club_b, opponent=club_a, result=result_b, match=match, season=season, settings_row=settings_row)

    _apply_battle_counters(db, club_a, result_a)
    _apply_battle_counters(db, club_b, result_b)

    _distribute_rewards(db, team_a, club_a, result_a, settings_row)
    _distribute_rewards(db, team_b, club_b, result_b, settings_row)

    achievement_service.check_and_grant(db, club_a, extra_flag="perfect_match" if _is_perfect_team(attempt_stats, team_a) else None)
    achievement_service.check_and_grant(db, club_b, extra_flag="perfect_match" if _is_perfect_team(attempt_stats, team_b) else None)

    result_label = {"win": "Victoire", "loss": "Défaite", "draw": "Match nul"}
    feed_service.record(db, club_id=club_a.id, event_type="club_battle_result", data={"result": result_a, "opponent_club_id": str(club_b.id), "score": cb.club_a_score, "opponent_score": cb.club_b_score, "match_id": str(match.id)})
    feed_service.record(db, club_id=club_b.id, event_type="club_battle_result", data={"result": result_b, "opponent_club_id": str(club_a.id), "score": cb.club_b_score, "opponent_score": cb.club_a_score, "match_id": str(match.id)})

    for p in team_a + team_b:
        result = result_a if p in team_a else result_b
        emit(
            db, event_type="club_battle_result", user_id=p.user_id, context={"result": result_label[result]},
            data={"match_id": str(match.id), "result": result}, dedup_key=f"club_battle_result:{match.id}:{p.user_id}",
        )
    if cb.mvp_user_id is not None:
        emit(
            db, event_type="club_battle_mvp_awarded", user_id=cb.mvp_user_id, data={"match_id": str(match.id)},
            dedup_key=f"club_battle_mvp_awarded:{match.id}",
        )

    anti_abuse_service.analyze_after_battle(db, club_id=club_a.id, opponent_club_id=club_b.id)
    anti_abuse_service.analyze_after_battle(db, club_id=club_b.id, opponent_club_id=club_a.id)


def _apply_club_rating(
    db: Session, club: Club, *, opponent: Club, result: str, match: CompetitiveMatch,
    season: Optional[ClubSeason], settings_row: PlatformSettings,
) -> None:
    before = club.rating
    if result == "win":
        delta = settings_row.competitive_club_rating_victory_gain
        reputation_service.award_battle_win_reputation(db, club)
    elif result == "loss":
        loss = settings_row.competitive_club_rating_defeat_loss
        if club.matches_played < settings_row.competitive_club_rating_protection_battles:
            loss = loss // 2
        delta = -loss
    else:
        delta = 0

    after = max(settings_row.competitive_club_rating_floor, before + delta)
    actual_delta = after - before
    club.rating = after
    db.add(club)
    db.commit()
    db.refresh(club)

    db.add(ClubRatingHistory(
        club_id=club.id, match_id=match.id, opponent_club_id=opponent.id,
        season_id=season.id if season else None, result=result,
        rating_before=before, rating_after=after, delta=actual_delta,
    ))
    db.commit()
    achievement_service.check_and_grant(db, club)


def _apply_battle_counters(db: Session, club: Club, result: str) -> None:
    club.matches_played += 1
    if result == "win":
        club.wins += 1
        club.current_streak = max(0, club.current_streak) + 1
    elif result == "loss":
        club.losses += 1
        club.current_streak = 0
    else:
        club.draws += 1
        club.current_streak = 0
    club.best_streak = max(club.best_streak, club.current_streak)
    db.add(club)
    db.commit()
    achievement_service.check_and_grant(db, club)


def _distribute_rewards(db: Session, team: List[CompetitiveMatchParticipant], club: Club, result: str, settings_row: PlatformSettings) -> None:
    from app.models.enums import KpSource
    from app.services import kp as kp_service

    ep = settings_row.competitive_club_battle_ep_reward_winner if result == "win" else settings_row.competitive_club_battle_ep_reward_participation
    xp = settings_row.competitive_club_battle_xp_reward_winner if result == "win" else settings_row.competitive_club_battle_xp_reward_participation

    if ep > 0:
        for p in team:
            try:
                kp_service.award_kp(p.user_id, ep, KpSource.competitive, "Bataille de club", db)
            except Exception:
                logger.exception("club battle EP reward failed user=%s", p.user_id)
        club.total_ep_earned = (club.total_ep_earned or 0) + ep * len(team)

    if xp > 0:
        club.xp = (club.xp or 0) + xp

    db.add(club)
    db.commit()


# ─── Admin oversight ────────────────────────────────────────────────────────

def admin_cancel_battle(db: Session, match: CompetitiveMatch, *, reason: Optional[str] = None, actor_email: Optional[str] = None) -> CompetitiveMatch:
    if match.match_type != "club_battle":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Ce match n'est pas une bataille de club.")
    if match.status in ("completed", "cancelled", "abandoned", "expired", "disconnected"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cette bataille est déjà terminée ou annulée.")

    match.cancelled_at = _now()
    match.cancelled_reason = reason
    # "in_progress"/"paused" have no direct -> 'cancelled' edge in
    # match_service.TRANSITIONS (only completed/disconnected/abandoned) —
    # 'abandoned' is the correct terminal state for an admin pulling the
    # plug mid-game; every earlier lobby state cancels normally.
    target_status = "abandoned" if match.status in ("in_progress", "paused") else "cancelled"
    match_service.transition(match, target_status)
    db.add(match)
    db.commit()
    db.refresh(match)
    event_log_service.log_event(db, match_id=match.id, actor_id=None, event_type="club_battle_admin_cancelled", meta={"reason": reason, "admin_email": actor_email, "target_status": target_status})
    return match


def list_all_battles(db: Session, *, status_filter: Optional[str] = None, page: int = 1, size: int = 20) -> Dict[str, Any]:
    query = select(ClubBattleMatch).order_by(ClubBattleMatch.created_at.desc())
    rows = list(db.exec(query.offset((page - 1) * size).limit(size)).all())
    items = []
    for cb in rows:
        match = db.get(CompetitiveMatch, cb.match_id)
        if match is None or (status_filter and match.status != status_filter):
            continue
        club_a = db.get(Club, cb.club_a_id)
        club_b = db.get(Club, cb.club_b_id)
        items.append({
            "match_id": cb.match_id, "status": match.status, "club_a_id": cb.club_a_id, "club_a_name": club_a.name if club_a else None,
            "club_b_id": cb.club_b_id, "club_b_name": club_b.name if club_b else None, "club_a_score": cb.club_a_score,
            "club_b_score": cb.club_b_score, "winner_club_id": cb.winner_club_id, "created_at": cb.created_at,
        })
    return {"items": items, "page": page, "size": size}
