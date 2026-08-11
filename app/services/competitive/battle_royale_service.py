from __future__ import annotations

"""
Battle Royale Manager — Competitive Arena Phase 10, Part A (Foundation).

Owns the Battle Royale waiting room / eligibility / config lifecycle.
Mirrors tournament_service.py's style deliberately (TRANSITIONS-style dict,
log_event helper via the existing per-match event_log_service — a Battle
Royale IS a single competitive_matches row, unlike a tournament which spans
many, so there is no need for a dedicated *_logs table the way
CompetitiveTournamentLog exists).

A Battle Royale reuses CompetitiveMatch (match_type='battle_royale') and
CompetitiveMatchParticipant VERBATIM as the one-row-per-match/one-row-per-
participant primitives — CompetitiveBattleRoyaleMatch is only a 1:1 side-
table of BR-specific config + live state.

Two parallel state machines exist for a live Battle Royale, and it matters
to keep them straight:
  - CompetitiveMatch.status: the SAME coarse status every match type shares
    (waiting_room -> countdown -> in_progress -> completed/cancelled/...,
    see match_service.TRANSITIONS). Part A only ever drives this up through
    'countdown' (see _begin_gameplay_countdown) — 'in_progress' onward is
    driven entirely by the EXISTING gameplay engine
    (gameplay_service.start_match, task_begin_match_after_countdown,
    task_gameplay_tick — see this module's own report for the exact
    hand-off boundary).
  - CompetitiveBattleRoyaleMatch.current_phase: a Battle-Royale-SPECIFIC,
    finer-grained gameplay phase (waiting_room -> countdown -> running ->
    final_phase -> final_duel -> completed, see
    BATTLE_ROYALE_PHASE_TRANSITIONS in app/models/competitive.py). Part A's
    own "waiting for enough players" fill-countdown lives entirely inside
    current_phase='countdown' while CompetitiveMatch.status is STILL
    'waiting_room' — only once the fill-countdown elapses (re-validated)
    does CompetitiveMatch.status itself advance to 'countdown' (the
    existing duel pre-start "get ready" countdown), at which point both
    state machines read 'countdown' simultaneously, briefly, until the
    existing engine flips CompetitiveMatch.status to 'in_progress'.

    IMPORTANT HANDOFF FOR PART B: nothing in this module ever sets
    current_phase='running' — that requires observing CompetitiveMatch.
    status become 'in_progress' for a battle_royale match, which happens
    inside gameplay_service.start_match() (out of this phase's scope, see
    the module's Task 2 instructions). Part B must add a small hook there
    (or in task_begin_match_after_countdown) that also flips the sibling
    competitive_battle_royale_matches.current_phase to 'running' at that
    moment. Until Part B does that, a completed fill-countdown leaves a BR
    match's current_phase sitting at 'countdown' forever even though real
    gameplay has begun — current_phase is informational for the waiting
    room only in Part A; CompetitiveMatch.status remains the authoritative
    signal for whether gameplay has actually started.

Elimination config contract — see app/models/competitive.py's module
docstring for the authoritative, spelled-out JSONB key contract per
elimination_mode (score_elimination/lives/survival). Part A never reads
these keys itself; it only validates elimination_mode+persists whatever
elimination_config blob the admin supplies.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.models.admin import PlatformSettings
from app.models.catalog import Level
from app.models.competitive import (
    BATTLE_ROYALE_PHASE_TRANSITIONS,
    CompetitiveBattleRoyaleMatch,
    CompetitiveBattleRoyaleRankingSnapshot,
    CompetitiveBattleRoyaleReward,
    CompetitiveMatch,
    CompetitiveMatchParticipant,
    CompetitiveMatchQuestion,
    CompetitiveQuestionAttempt,
    CompetitiveRewardGrant,
    CompetitiveStatistics,
)
from app.models.profile import Profile, StudentProfile
from app.services.competitive import event_log_service, match_service
from app.services.notification_engine import emit

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─── current_phase state machine ────────────────────────────────────────────

def transition_phase(br: CompetitiveBattleRoyaleMatch, new_phase: str) -> CompetitiveBattleRoyaleMatch:
    allowed = BATTLE_ROYALE_PHASE_TRANSITIONS.get(br.current_phase, set())
    if new_phase not in allowed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Transition de phase invalide : {br.current_phase} -> {new_phase}",
        )
    br.current_phase = new_phase
    br.updated_at = _now()
    return br


def get_battle_royale_or_404(db: Session, match_id: UUID) -> Tuple[CompetitiveMatch, CompetitiveBattleRoyaleMatch]:
    match = match_service.get_match_or_404(db, match_id)
    if match.match_type != "battle_royale":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ce match n'est pas un Battle Royale.")
    br = db.get(CompetitiveBattleRoyaleMatch, match_id)
    if br is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Configuration Battle Royale introuvable.")
    return match, br


# ─── Eligibility ─────────────────────────────────────────────────────────────

def check_battle_royale_eligibility(
    db: Session, match: CompetitiveMatch, br: CompetitiveBattleRoyaleMatch, user_id: UUID,
) -> None:
    """Raises HTTPException on failure. Authentication itself is already
    guaranteed by the router's Depends(get_current_user) — mirrors
    tournament_service.check_registration_eligibility's exact structure."""
    already_here = db.exec(
        select(CompetitiveMatchParticipant)
        .where(CompetitiveMatchParticipant.match_id == match.id)
        .where(CompetitiveMatchParticipant.user_id == user_id)
        .where(CompetitiveMatchParticipant.left_at.is_(None))
    ).first()
    if already_here is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tu participes déjà à ce Battle Royale.")

    stats = db.get(CompetitiveStatistics, user_id)
    if stats is not None and stats.suspended_until is not None and stats.suspended_until > _now():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Ton accès à l'Arène compétitive est suspendu.")

    # Academic level compatibility — same Level.code / StudentProfile.
    # level_main prefix-match interpretation as tournament_service.
    # check_registration_eligibility (see that function's own comment for
    # why this is the reliable join available given the current schema).
    if match.school_level_id is not None:
        level = db.get(Level, match.school_level_id)
        student = db.get(StudentProfile, user_id)
        if level is not None:
            if student is None or student.level_main is None or not level.code.startswith(f"{student.level_main}_"):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Ce Battle Royale est réservé à un autre niveau scolaire.")

    # "One active Battle Royale per player": any OTHER match this user is
    # still an active participant of, whose sibling BR config isn't yet
    # 'completed'.
    active_elsewhere = db.exec(
        select(CompetitiveMatchParticipant)
        .join(CompetitiveBattleRoyaleMatch, CompetitiveBattleRoyaleMatch.match_id == CompetitiveMatchParticipant.match_id)
        .where(CompetitiveMatchParticipant.user_id == user_id)
        .where(CompetitiveMatchParticipant.left_at.is_(None))
        .where(CompetitiveMatchParticipant.match_id != match.id)
        .where(CompetitiveBattleRoyaleMatch.current_phase != "completed")
    ).first()
    if active_elsewhere is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tu participes déjà à un autre Battle Royale actif.")


# ─── Creation ────────────────────────────────────────────────────────────────

def create_battle_royale_match(
    db: Session,
    *,
    creator_id: Optional[UUID] = None,
    subject_ids: Optional[List[UUID]] = None,
    school_level_id: Optional[UUID] = None,
    difficulty: Optional[str] = None,
    question_count: int = 10,
    min_players: int,
    max_players: int,
    elimination_mode: str,
    elimination_config: Optional[Dict[str, Any]] = None,
    final_phase_threshold: int = 5,
    final_duel_method: str = "total_score",
    tie_break_rules: Optional[Dict[str, Any]] = None,
    disconnect_policy: str = "auto_eliminate",
    disconnect_grace_seconds: int = 30,
    ranking_impact: bool = True,
    auto_start_countdown_seconds: int = 30,
    visibility: str = "public",
    max_spectators: Optional[int] = None,
    entry_cost_ep: Optional[int] = None,
    actor_email: Optional[str] = None,
) -> CompetitiveMatch:
    """Creates the CompetitiveMatch row (match_type='battle_royale', status
    lands directly in 'waiting_room' — an open lobby has no inviter/invitee
    step, unlike a duel's draft/waiting_for_opponent/accepted flow; see
    match_service.TRANSITIONS's widened 'draft' -> 'waiting_room' edge) plus
    the 1:1 config row. If creator_id is given, the creator auto-joins as
    the first participant (mirrors match_service.create_match); if None,
    this is a pure admin-created lobby with zero players until the first
    /join call."""
    if min_players > max_players:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="min_players doit être inférieur ou égal à max_players.")

    match = CompetitiveMatch(
        match_type="battle_royale", creator_id=creator_id, created_by=creator_id,
        subject_ids=subject_ids or [], school_level_id=school_level_id, difficulty=difficulty,
        question_count=question_count, visibility=visibility, max_players=max_players,
        max_spectators=max_spectators,
    )
    db.add(match)
    db.commit()
    db.refresh(match)

    match_service.transition(match, "waiting_room")
    db.add(match)
    db.commit()
    db.refresh(match)

    br = CompetitiveBattleRoyaleMatch(
        match_id=match.id, min_players=min_players, max_players=max_players,
        elimination_mode=elimination_mode, elimination_config=elimination_config or {},
        final_phase_threshold=final_phase_threshold, final_duel_method=final_duel_method,
        tie_break_rules=tie_break_rules or {}, disconnect_policy=disconnect_policy,
        disconnect_grace_seconds=disconnect_grace_seconds, ranking_impact=ranking_impact,
        auto_start_countdown_seconds=auto_start_countdown_seconds, entry_cost_ep=entry_cost_ep,
        current_phase="waiting_room", remaining_players_count=0,
    )
    db.add(br)
    db.commit()
    db.refresh(br)

    event_log_service.log_event(
        db, match_id=match.id, actor_id=creator_id, event_type="battle_royale_created",
        meta={
            "min_players": min_players, "max_players": max_players, "elimination_mode": elimination_mode,
            "admin_email": actor_email,
        },
    )

    if creator_id is not None:
        join_battle_royale(db, match, br, creator_id)
        db.refresh(match)

    return match


# ─── Waiting room: join / leave / auto-start ───────────────────────────────

def join_battle_royale(
    db: Session, match: CompetitiveMatch, br: CompetitiveBattleRoyaleMatch, user_id: UUID,
) -> CompetitiveMatchParticipant:
    """Joining is allowed during BOTH 'waiting_room' and 'countdown' (a
    player may still join while the fill-countdown ticks down — only once
    real gameplay begins does the lobby close). No waiting list — a full
    lobby is simply a 409, unlike tournaments (the spec doesn't call for one
    here; simplest/most conservative reading, see this phase's report)."""
    if br.current_phase not in ("waiting_room", "countdown") or match.status not in ("waiting_room", "countdown"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce Battle Royale n'accepte plus de nouveaux joueurs.")

    check_battle_royale_eligibility(db, match, br, user_id)

    current_count = br.remaining_players_count or 0
    if current_count >= br.max_players:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce Battle Royale est complet.")

    participant = CompetitiveMatchParticipant(match_id=match.id, user_id=user_id)
    db.add(participant)
    db.commit()
    db.refresh(participant)

    new_count = current_count + 1
    br.remaining_players_count = new_count
    br.updated_at = _now()
    db.add(br)
    db.commit()
    db.refresh(br)

    event_log_service.log_event(
        db, match_id=match.id, actor_id=user_id, event_type="battle_royale_joined",
        meta={"joined_count": new_count, "min_players": br.min_players, "max_players": br.max_players},
    )

    if new_count == br.max_players and br.current_phase in ("waiting_room", "countdown"):
        # Lobby just filled completely — end any wait immediately (early
        # start), regardless of whether the fill-countdown had even started.
        begin_gameplay_countdown(db, match, br)
    elif new_count == br.min_players and br.current_phase == "waiting_room":
        _start_autostart_countdown(db, match, br)

    return participant


def leave_battle_royale(db: Session, match: CompetitiveMatch, br: CompetitiveBattleRoyaleMatch, user_id: UUID) -> None:
    """Only allowed while current_phase='waiting_room' — once the fill-
    countdown starts (or gameplay itself), leaving becomes a forfeit/
    elimination concern, which belongs to Part B's live engine, not this
    module."""
    if br.current_phase != "waiting_room":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Il n'est plus possible de quitter ce Battle Royale une fois le compte à rebours lancé.",
        )
    participant = db.exec(
        select(CompetitiveMatchParticipant)
        .where(CompetitiveMatchParticipant.match_id == match.id)
        .where(CompetitiveMatchParticipant.user_id == user_id)
        .where(CompetitiveMatchParticipant.left_at.is_(None))
    ).first()
    if participant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tu ne participes pas à ce Battle Royale.")

    participant.left_at = _now()
    db.add(participant)
    db.commit()

    br.remaining_players_count = max(0, (br.remaining_players_count or 1) - 1)
    br.updated_at = _now()
    db.add(br)
    db.commit()

    event_log_service.log_event(
        db, match_id=match.id, actor_id=user_id, event_type="battle_royale_left",
        meta={"remaining_players_count": br.remaining_players_count},
    )


def _start_autostart_countdown(db: Session, match: CompetitiveMatch, br: CompetitiveBattleRoyaleMatch) -> None:
    """Called exactly once, the moment remaining_players_count first reaches
    min_players while still 'waiting_room'. Flips current_phase to
    'countdown' (BR's OWN fill-wait countdown — CompetitiveMatch.status
    stays 'waiting_room' throughout this) and schedules a one-off Celery
    re-validation task, mirroring the existing disconnect-grace one-off task
    pattern (apply_async(countdown=...))."""
    transition_phase(br, "countdown")
    db.add(br)
    db.commit()
    db.refresh(br)

    event_log_service.log_event(
        db, match_id=match.id, actor_id=None, event_type="battle_royale_autostart_countdown_started",
        meta={"auto_start_countdown_seconds": br.auto_start_countdown_seconds},
    )

    try:
        from app.workers.competitive_tasks import task_battle_royale_autostart_check
        task_battle_royale_autostart_check.apply_async(args=[str(match.id)], countdown=br.auto_start_countdown_seconds)
    except Exception:
        logger.exception("failed to schedule battle royale autostart check for match=%s", match.id)

    for p in match_service.get_participants(db, match.id):
        emit(
            db, event_type="battle_royale_waiting_room_ready", user_id=p.user_id,
            data={"match_id": str(match.id)},
            dedup_key=f"battle_royale_waiting_room_ready:{match.id}",
        )


def revert_autostart_countdown(db: Session, match: CompetitiveMatch, br: CompetitiveBattleRoyaleMatch) -> None:
    """Called by task_battle_royale_autostart_check when the fill-countdown
    elapses but the player count has since dropped back below min_players
    (someone left during the countdown). Falls back to 'waiting_room' rather
    than cancelling the match outright — a simple, conservative choice: the
    lobby just keeps waiting for more joiners, and the next join that
    crosses min_players again re-triggers a fresh countdown."""
    transition_phase(br, "waiting_room")
    db.add(br)
    db.commit()
    db.refresh(br)

    event_log_service.log_event(
        db, match_id=match.id, actor_id=None, event_type="battle_royale_autostart_countdown_reverted",
        meta={"remaining_players_count": br.remaining_players_count},
    )


def begin_gameplay_countdown(db: Session, match: CompetitiveMatch, br: CompetitiveBattleRoyaleMatch) -> None:
    """THE handoff point into the EXISTING duel gameplay engine — called
    either once the fill-countdown elapses re-validated (via
    task_battle_royale_autostart_check), or immediately when the lobby
    fills completely (early start, from join_battle_royale), or by an
    admin's force-start action. Transitions CompetitiveMatch.status
    'waiting_room' -> 'countdown' and schedules task_begin_match_after_
    countdown EXACTLY like app/routers/competitive/lobby.py's set_ready does
    for a duel — from this point on, gameplay start (question generation,
    'in_progress' transition, current_question_position, everything) is
    entirely the EXISTING gameplay_service/competitive_tasks machinery.
    Nothing below this function is Battle-Royale-specific.

    Defensive no-op if match.status has already moved past 'waiting_room'
    (double-fire guard: e.g. the fill-countdown task and an admin's force-
    start racing) — same self-healing idiom used throughout this codebase
    (gameplay_service.ensure_progressed, task_begin_match_after_countdown's
    own status check)."""
    if br.current_phase == "waiting_room":
        transition_phase(br, "countdown")
        db.add(br)
        db.commit()
        db.refresh(br)

    if match.status != "waiting_room":
        return  # already progressed — no-op

    match_service.transition(match, "countdown")
    db.add(match)
    db.commit()
    db.refresh(match)

    settings_row = db.get(PlatformSettings, True) or PlatformSettings()
    try:
        from app.workers.competitive_tasks import task_begin_match_after_countdown
        task_begin_match_after_countdown.apply_async(
            args=[str(match.id)], countdown=settings_row.competitive_match_countdown_seconds,
        )
    except Exception:
        logger.exception("failed to schedule gameplay start for battle royale match=%s", match.id)

    event_log_service.log_event(db, match_id=match.id, actor_id=None, event_type="battle_royale_gameplay_countdown_started", meta={})

    for p in match_service.get_participants(db, match.id):
        emit(
            db, event_type="battle_royale_match_starting", user_id=p.user_id,
            data={"match_id": str(match.id)},
            dedup_key=f"battle_royale_match_starting:{match.id}",
        )


def get_waiting_room(db: Session, match: CompetitiveMatch, br: CompetitiveBattleRoyaleMatch) -> Dict[str, Any]:
    participants = match_service.get_participants(db, match.id)
    profiles = (
        {p.id: p for p in db.exec(select(Profile).where(Profile.id.in_({pp.user_id for pp in participants}))).all()}
        if participants else {}
    )

    countdown_deadline = None
    if br.current_phase == "countdown":
        countdown_deadline = br.updated_at + timedelta(seconds=br.auto_start_countdown_seconds)

    return {
        "match_id": match.id, "match_status": match.status, "current_phase": br.current_phase,
        "subject_ids": match.subject_ids, "school_level_id": match.school_level_id, "difficulty": match.difficulty,
        "min_players": br.min_players, "max_players": br.max_players,
        "joined_count": len(participants),
        "countdown_deadline": countdown_deadline,
        "estimated_start_at": countdown_deadline,
        "players": [
            {
                "id": p.id, "match_id": p.match_id, "user_id": p.user_id,
                "full_name": profiles[p.user_id].full_name if p.user_id in profiles else None,
                "avatar_url": profiles[p.user_id].avatar_url if p.user_id in profiles else None,
                "is_ready": p.is_ready, "score": p.score, "lives": p.lives,
                "elimination_round": p.elimination_round, "final_rank": p.final_rank,
                "eliminated_at": p.eliminated_at, "joined_at": p.joined_at, "left_at": p.left_at,
            }
            for p in participants
        ],
    }


# ─── Read (list/detail) ─────────────────────────────────────────────────────

def list_battle_royales_admin(db: Session, *, phase_filter: Optional[str] = None) -> List[Tuple[CompetitiveMatch, CompetitiveBattleRoyaleMatch]]:
    query = select(CompetitiveMatch).where(CompetitiveMatch.match_type == "battle_royale").where(CompetitiveMatch.deleted_at.is_(None))
    matches = list(db.exec(query.order_by(CompetitiveMatch.created_at.desc())).all())
    out = []
    for m in matches:
        br = db.get(CompetitiveBattleRoyaleMatch, m.id)
        if br is None:
            continue
        if phase_filter and br.current_phase != phase_filter:
            continue
        out.append((m, br))
    return out


def list_battle_royales_public(
    db: Session, *, subject_id: Optional[UUID] = None, school_level_id: Optional[UUID] = None,
    difficulty: Optional[str] = None, max_players: Optional[int] = None,
) -> List[Tuple[CompetitiveMatch, CompetitiveBattleRoyaleMatch]]:
    """Never shows draft/cancelled/expired matches — player-facing list
    only (mirrors tournament_service.list_tournaments_public's own
    filtering philosophy)."""
    query = (
        select(CompetitiveMatch)
        .where(CompetitiveMatch.match_type == "battle_royale")
        .where(CompetitiveMatch.deleted_at.is_(None))
        .where(CompetitiveMatch.status.not_in(("draft", "cancelled", "expired")))
        .where(CompetitiveMatch.visibility == "public")
    )
    if difficulty:
        query = query.where(CompetitiveMatch.difficulty == difficulty)
    if school_level_id:
        query = query.where(CompetitiveMatch.school_level_id == school_level_id)
    if max_players:
        query = query.where(CompetitiveMatch.max_players == max_players)
    matches = list(db.exec(query.order_by(CompetitiveMatch.created_at.desc())).all())

    out = []
    for m in matches:
        if subject_id and subject_id not in (m.subject_ids or []):
            continue
        br = db.get(CompetitiveBattleRoyaleMatch, m.id)
        if br is None:
            continue
        out.append((m, br))
    return out


def admin_list_participants(db: Session, match_id: UUID) -> List[CompetitiveMatchParticipant]:
    """Admin view — every status, INCLUDING players who already left
    (left_at IS NOT NULL), unlike match_service.get_participants."""
    return list(
        db.exec(
            select(CompetitiveMatchParticipant)
            .where(CompetitiveMatchParticipant.match_id == match_id)
            .order_by(CompetitiveMatchParticipant.joined_at.asc())
        ).all()
    )


# ─── Admin CRUD ──────────────────────────────────────────────────────────────

_MATCH_FIELDS = {"subject_ids", "school_level_id", "difficulty", "question_count", "visibility", "max_spectators"}
_BR_FIELDS = {
    "min_players", "max_players", "elimination_mode", "elimination_config", "final_phase_threshold",
    "final_duel_method", "tie_break_rules", "disconnect_policy", "disconnect_grace_seconds",
    "ranking_impact", "auto_start_countdown_seconds", "entry_cost_ep",
}


def update_battle_royale(
    db: Session, match: CompetitiveMatch, br: CompetitiveBattleRoyaleMatch, *, actor_email: Optional[str] = None, **fields,
) -> Tuple[CompetitiveMatch, CompetitiveBattleRoyaleMatch]:
    """Only allowed while current_phase='waiting_room' — once the fill-
    countdown (or real gameplay) has started, config is locked, same
    conservative cutoff as tournament_service.update_tournament."""
    if br.current_phase != "waiting_room":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce Battle Royale ne peut plus être modifié (compte à rebours ou partie déjà lancée).")

    updates = {k: v for k, v in fields.items() if v is not None}
    for key, value in updates.items():
        if key in _MATCH_FIELDS:
            setattr(match, key, value)
        elif key in _BR_FIELDS:
            setattr(br, key, value)
    if "max_players" in updates:
        match.max_players = updates["max_players"]  # kept in sync — CompetitiveMatch.max_players is read by other subsystems (spectator WS, etc.)
    if br.min_players > br.max_players:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="min_players doit être inférieur ou égal à max_players.")

    match.updated_at = _now()
    br.updated_at = _now()
    db.add(match)
    db.add(br)
    db.commit()
    db.refresh(match)
    db.refresh(br)

    event_log_service.log_event(
        db, match_id=match.id, actor_id=None, event_type="battle_royale_updated",
        meta={"fields": list(updates.keys()), "admin_email": actor_email},
    )
    return match, br


def cancel_battle_royale(db: Session, match: CompetitiveMatch, br: CompetitiveBattleRoyaleMatch, *, actor_email: Optional[str] = None, reason: Optional[str] = None) -> CompetitiveMatch:
    if match.status in ("completed", "cancelled", "expired", "abandoned"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce Battle Royale est déjà terminé.")

    match_service.transition(match, "cancelled")
    match.cancelled_at = _now()
    match.cancelled_reason = reason or "Battle Royale annulé par un administrateur."
    db.add(match)
    db.commit()
    db.refresh(match)

    event_log_service.log_event(
        db, match_id=match.id, actor_id=None, event_type="battle_royale_cancelled",
        meta={"reason": reason, "admin_email": actor_email},
    )
    return match


def force_start_battle_royale(db: Session, match: CompetitiveMatch, br: CompetitiveBattleRoyaleMatch, *, actor_email: Optional[str] = None) -> CompetitiveMatch:
    """Admin override — start immediately even if min_players hasn't been
    reached yet (or skip the remaining fill-countdown wait). Requires at
    least 2 joined players (a conservative floor — a "battle" of 1 makes no
    sense); hands off to the exact same begin_gameplay_countdown every
    other start path uses."""
    if br.current_phase not in ("waiting_room", "countdown"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce Battle Royale a déjà démarré ou est terminé.")
    joined_count = br.remaining_players_count or 0
    if joined_count < 2:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Il faut au moins 2 joueurs pour démarrer un Battle Royale.")

    event_log_service.log_event(
        db, match_id=match.id, actor_id=None, event_type="battle_royale_force_started",
        meta={"admin_email": actor_email, "joined_count": joined_count},
    )
    begin_gameplay_countdown(db, match, br)
    db.refresh(match)
    db.refresh(br)
    return match


def pause_battle_royale(db: Session, match: CompetitiveMatch, br: CompetitiveBattleRoyaleMatch, *, actor_email: Optional[str] = None) -> CompetitiveMatch:
    """Only meaningful once current_phase is an active gameplay phase.
    Reuses CompetitiveMatch.status='paused' VERBATIM — task_gameplay_tick
    (app/workers/competitive_tasks.py) already no-ops whenever
    match.status != 'in_progress', so simply transitioning the status is
    100% sufficient to freeze the tick loop; Part B needs to add NOTHING
    extra to respect a pause. current_phase itself is left untouched (still
    'running'/'final_phase'/'final_duel') — it describes the gameplay
    SUB-PHASE, not whether the clock is currently ticking."""
    if br.current_phase not in ("running", "final_phase", "final_duel"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cette action n'est possible que pendant une partie en cours.")
    if match.status != "in_progress":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce match n'est pas en cours.")

    match_service.transition(match, "paused")
    db.add(match)
    db.commit()
    db.refresh(match)

    event_log_service.log_event(db, match_id=match.id, actor_id=None, event_type="battle_royale_paused", meta={"admin_email": actor_email})
    return match


def resume_battle_royale(db: Session, match: CompetitiveMatch, br: CompetitiveBattleRoyaleMatch, *, actor_email: Optional[str] = None) -> CompetitiveMatch:
    """See pause_battle_royale's docstring — the mirror action. NOTE for
    Part B: resuming does NOT itself re-schedule a fresh task_gameplay_tick
    — whichever tick was already in flight before the pause will simply
    fire late and self-heal via gameplay_service.ensure_progressed() the
    same way every other lagged tick does, per this codebase's existing
    self-healing convention. If Part B's BR tick logic needs a MORE
    aggressive immediate catch-up on resume (rather than waiting for the
    stale tick), that's a deliberate addition for Part B to make here, not
    an oversight."""
    if match.status != "paused":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce match n'est pas en pause.")

    match_service.transition(match, "in_progress")
    db.add(match)
    db.commit()
    db.refresh(match)

    event_log_service.log_event(db, match_id=match.id, actor_id=None, event_type="battle_royale_resumed", meta={"admin_email": actor_email})
    return match


# ─── Rewards config CRUD ─────────────────────────────────────────────────────

def list_rewards(db: Session, match_id: UUID) -> List[CompetitiveBattleRoyaleReward]:
    return list(
        db.exec(select(CompetitiveBattleRoyaleReward).where(CompetitiveBattleRoyaleReward.match_id == match_id)).all()
    )


def create_reward(
    db: Session, match_id: UUID, *, actor_email: Optional[str] = None, rank_min: int, rank_max: int,
    reward_type: str, reward_ref: Optional[str], reward_amount: Optional[int],
) -> CompetitiveBattleRoyaleReward:
    reward = CompetitiveBattleRoyaleReward(
        match_id=match_id, rank_min=rank_min, rank_max=rank_max, reward_type=reward_type,
        reward_ref=reward_ref, reward_amount=reward_amount,
    )
    db.add(reward)
    db.commit()
    db.refresh(reward)
    event_log_service.log_event(
        db, match_id=match_id, actor_id=None, event_type="battle_royale_reward_created",
        meta={"rank_min": rank_min, "rank_max": rank_max, "reward_type": reward_type, "admin_email": actor_email},
    )
    return reward


def delete_reward(db: Session, match_id: UUID, reward_id: UUID, *, actor_email: Optional[str] = None) -> None:
    reward = db.get(CompetitiveBattleRoyaleReward, reward_id)
    if reward is None or reward.match_id != match_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Récompense introuvable pour ce Battle Royale.")
    db.delete(reward)
    db.commit()
    event_log_service.log_event(
        db, match_id=match_id, actor_id=None, event_type="battle_royale_reward_deleted",
        meta={"reward_id": str(reward_id), "admin_email": actor_email},
    )


# ─── Phase 10 — Battle Royale, Part B (live gameplay engine) ────────────────
#
# Everything below is new in Part B. Two hand-offs from gameplay_service.py
# call into this module (both lazily imported there, mirroring how
# gameplay_service.finish_match already lazily imports spectator_service —
# this avoids a module-load-time circular import, since gameplay_service
# also needs this module at its own module level... actually it does NOT:
# every call site below is a lazy, in-function import on the gameplay_
# service side, so this module never needs to import gameplay_service at
# its own top level either — only lazily, inside _finalize_battle_royale /
# complete_from_exhausted_questions, right where finish_match() is actually
# invoked):
#   1. gameplay_service.start_match() -> handle_gameplay_started() the
#      instant CompetitiveMatch.status flips to 'in_progress'.
#   2. gameplay_service._lock_and_reveal() -> process_elimination_tick()
#      right after each question's scoring is committed (own try/except
#      there, mirrors the Phase 8 spectator-timeline block in the same
#      function).
#   3. gameplay_service.ensure_progressed() -> complete_from_exhausted_
#      questions() INSTEAD of the generic finish_match(db, match) once a BR
#      match's question list runs out with >= 2 players still active (the
#      generic 2-player-or-draw completion logic doesn't know how to pick
#      an N-player winner).
#
# Tie-break rule used for EVERY in-match ranking decision (leaderboard
# snapshots, score_elimination target selection, provisional final_rank for
# players eliminated mid-match): score DESC, then fewer cumulative wrong
# answers ASC, then earlier match join time (joined_at) ASC. Spelled out
# once here (rank_key()) — Part C should reuse this exact rule for anything
# that needs to reconstruct/verify in-match standings.
#
# A SEPARATE, narrower tie-break applies only to final_duel_method's own
# winner determination when the match's questions run out with >= 2 players
# still active (final_duel_key()) — see _determine_final_duel_winner()'s
# docstring for the exact contract of tie_break_rules.


def handle_gameplay_started(db: Session, match: CompetitiveMatch) -> None:
    """Called once from gameplay_service.start_match(), right after
    CompetitiveMatch.status flips 'countdown' -> 'in_progress' for a
    battle_royale match (see that function's own docstring/Part A's module
    docstring for the full hand-off explanation). Flips current_phase
    'countdown' -> 'running', freezes remaining_players_count at the actual
    starting roster (may differ slightly from the last waiting-room count
    if the fill-countdown/gameplay-countdown window allowed a late leave —
    though Part A's leave_battle_royale already blocks leaving past
    'waiting_room', so this is mostly a defensive re-count), and — for
    elimination_mode='lives' — copies the starting lives count onto every
    participant.

    Deliberately NOT wrapped in try/except by the caller: a failure here
    means a Battle Royale started gameplay without its live state properly
    initialized (current_phase stuck at 'countdown', lives never set), which
    is far worse silently swallowed than surfaced immediately at match
    start."""
    br = db.get(CompetitiveBattleRoyaleMatch, match.id)
    if br is None:
        return  # not actually a configured BR match — nothing to do

    if br.current_phase == "countdown":
        transition_phase(br, "running")

    participants = match_service.get_participants(db, match.id)
    br.remaining_players_count = len(participants)
    db.add(br)

    if br.elimination_mode == "lives":
        starting_lives = int((br.elimination_config or {}).get("lives") or 3)
        for p in participants:
            p.lives = starting_lives
            db.add(p)

    db.commit()
    db.refresh(br)

    event_log_service.log_event(
        db, match_id=match.id, actor_id=None, event_type="battle_royale_gameplay_started",
        meta={"remaining_players_count": br.remaining_players_count, "elimination_mode": br.elimination_mode},
    )


def bulk_attempt_stats(db: Session, match_id: UUID) -> Dict[UUID, Dict[str, Any]]:
    """One query — {user_id: {"correct", "wrong", "avg_response_time_ms",
    "accuracy_pct"}} across every question attempted so far this match.
    Shared by the ranking/elimination tie-break (rank_key, only reads
    "wrong") and the final-duel tie-break (final_duel_key, reads the full
    dict) — also reused by gameplay_service.compute_battle_royale_state for
    the live leaderboard ordering, so it isn't prefixed private."""
    rows = list(
        db.exec(
            select(CompetitiveQuestionAttempt)
            .join(CompetitiveMatchQuestion, CompetitiveMatchQuestion.id == CompetitiveQuestionAttempt.match_question_id)
            .where(CompetitiveMatchQuestion.match_id == match_id)
        ).all()
    )
    by_user: Dict[UUID, List[CompetitiveQuestionAttempt]] = {}
    for a in rows:
        by_user.setdefault(a.user_id, []).append(a)

    out: Dict[UUID, Dict[str, Any]] = {}
    for uid, attempts in by_user.items():
        correct = sum(1 for a in attempts if a.is_correct)
        wrong = sum(1 for a in attempts if not a.is_correct)
        times = [a.response_time_ms for a in attempts if a.response_time_ms is not None]
        total = correct + wrong
        out[uid] = {
            "correct": correct, "wrong": wrong,
            "avg_response_time_ms": round(sum(times) / len(times)) if times else None,
            "accuracy_pct": round((correct / total) * 100, 2) if total else 0.0,
        }
    return out


def rank_key(p: CompetitiveMatchParticipant, stats: Dict[UUID, Dict[str, Any]]) -> Tuple[int, int, Any]:
    """THE deterministic in-match tie-break rule (see this section's module
    comment): score DESC, fewer cumulative wrong answers ASC, earlier
    joined_at ASC."""
    s = stats.get(p.user_id, {})
    return (-p.score, s.get("wrong", 0), p.joined_at)


def _attempts_for_question(db: Session, match_question_id: UUID) -> List[CompetitiveQuestionAttempt]:
    return list(
        db.exec(
            select(CompetitiveQuestionAttempt).where(CompetitiveQuestionAttempt.match_question_id == match_question_id)
        ).all()
    )


def _write_ranking_snapshot(
    db: Session, match: CompetitiveMatch, position: int, active: List[CompetitiveMatchParticipant],
    stats: Dict[UUID, Dict[str, Any]],
) -> List[CompetitiveMatchParticipant]:
    """Writes one CompetitiveBattleRoyaleRankingSnapshot row per currently-
    ACTIVE participant for this question_position, ranked via rank_key().
    Returns the ranked list (best first) so callers needn't re-sort."""
    ranked = sorted(active, key=lambda p: rank_key(p, stats))
    for idx, p in enumerate(ranked, start=1):
        db.add(CompetitiveBattleRoyaleRankingSnapshot(
            match_id=match.id, question_position=position, user_id=p.user_id, rank=idx, score=p.score,
        ))
    db.commit()
    return ranked


def _apply_eliminations(
    db: Session, match: CompetitiveMatch, br: CompetitiveBattleRoyaleMatch,
    active_before: List[CompetitiveMatchParticipant], eliminated_ids: set,
    position: int, stats: Dict[UUID, Dict[str, Any]],
) -> None:
    """Finalizes elimination bookkeeping for every participant eliminated
    THIS tick: eliminated_at/elimination_round, a PROVISIONAL final_rank
    (worst-ranked-among-active gets the worst number — e.g. 100 active ->
    80 after this tick means the 20 eliminated together get ranks 81-100,
    ordered among themselves by rank_key(), best-of-the-eliminated getting
    81), an event_log_service row, and a WS broadcast so the frontend can
    transition that player to spectator mode.

    NOTE FOR PART C: these provisional final_rank values are ALREADY
    AUTHORITATIVE and must NOT be recomputed/renumbered later — they
    reflect the true elimination order and never collide with the ranks
    finalized at match completion (1..remaining_at_completion), since every
    number assigned here is strictly greater than remaining_at_completion
    could ever be at the moment this tick ran."""
    if not eliminated_ids:
        return
    eliminated_participants = [p for p in active_before if p.user_id in eliminated_ids]
    ranked = sorted(eliminated_participants, key=lambda p: rank_key(p, stats))  # best-of-the-eliminated first
    n_before = len(active_before)
    start_rank = n_before - len(ranked) + 1

    now = _now()
    for idx, p in enumerate(ranked):
        p.eliminated_at = now
        p.elimination_round = position
        p.final_rank = start_rank + idx
        db.add(p)
    db.commit()

    for p in ranked:
        event_log_service.log_event(
            db, match_id=match.id, actor_id=None, event_type="battle_royale_eliminated",
            meta={
                "user_id": str(p.user_id), "final_rank": p.final_rank,
                "question_position": position, "elimination_mode": br.elimination_mode,
            },
        )
        try:
            from app.core.competitive_ws import publish_competitive_event
            # No per-connection addressing exists in the existing WS infra
            # (app/main.py's /ws/competitive/{match_id} channel only ever
            # broadcasts to every connection registered under this match_id,
            # personalizing per-viewer ONLY via the separate "gameplay_
            # update"-triggers-a-recomputed-push mechanism) — so this event
            # is broadcast to every connected player of this match, exactly
            # like "participant_connected"/"participant_disconnected"
            # already are, WITH an explicit user_id field for the frontend
            # to filter on client-side.
            publish_competitive_event(str(match.id), {
                "type": "battle_royale_eliminated", "user_id": str(p.user_id),
                "final_rank": p.final_rank, "question_position": position,
            })
        except Exception:
            logger.debug("battle royale eliminated WS push failed match=%s user=%s", match.id, p.user_id, exc_info=True)

        # Task 7 — notification: the eliminated player themself.
        emit(
            db, event_type="battle_royale_eliminated", user_id=p.user_id,
            context={"rank": p.final_rank},
            data={"match_id": str(match.id), "final_rank": p.final_rank, "question_position": position},
            dedup_key=f"battle_royale_eliminated:{match.id}:{p.user_id}",
        )


def process_elimination_tick(db: Session, match: CompetitiveMatch, mq: CompetitiveMatchQuestion) -> None:
    """Part B's core new logic — called from gameplay_service._lock_and_
    reveal() right after this question's scoring is committed (own try/
    except at the call site). Recomputes the leaderboard, applies the
    configured elimination_mode, cascades phase progression / completion,
    and republishes 'gameplay_update' so every remaining connected player's
    next personalized state push reflects the new standings."""
    br = db.get(CompetitiveBattleRoyaleMatch, match.id)
    if br is None or br.current_phase == "completed":
        return

    position = mq.position
    participants = match_service.get_participants(db, match.id)
    active = [p for p in participants if p.elimination_round is None]
    if not active:
        return

    this_q_attempts = {a.user_id: a for a in _attempts_for_question(db, mq.id)}

    # elimination_mode='lives' — decrement BEFORE the ranking snapshot/
    # elimination-target computation below, so both the snapshot and a
    # same-tick 0-lives elimination see the POST-decrement lives count.
    # Missing an attempt entirely (no CompetitiveQuestionAttempt row this
    # question) counts as wrong/unanswered, same as an incorrect answer.
    if br.elimination_mode == "lives":
        for p in active:
            attempt = this_q_attempts.get(p.user_id)
            if attempt is None or not attempt.is_correct:
                p.lives = max(0, (p.lives if p.lives is not None else 0) - 1)
                db.add(p)
        db.commit()

    stats = bulk_attempt_stats(db, match.id)
    ranked_active = _write_ranking_snapshot(db, match, position, active, stats)

    eliminated_ids: set = set()
    if br.elimination_mode == "score_elimination":
        cfg = br.elimination_config or {}
        interval = int(cfg.get("interval_questions") or 1)
        pct = int(cfg.get("eliminate_pct") or 0)
        if interval > 0 and pct > 0 and (position + 1) % interval == 0:
            active_count = len(active)
            # Boundary rule: never cut below final_phase_threshold remaining
            # in one elimination pass. If the roster is already AT or below
            # the threshold, skip elimination entirely this tick (the
            # final_phase/final_duel/completion cascade below already
            # handles being at/under threshold without a further cut) —
            # otherwise cap eliminate_pct's raw count so remaining never
            # drops below final_phase_threshold in this single tick.
            if active_count > br.final_phase_threshold:
                n_eliminate = max(1, round(active_count * pct / 100))
                n_eliminate = min(n_eliminate, active_count - br.final_phase_threshold)
                losers = ranked_active[-n_eliminate:] if n_eliminate > 0 else []
                eliminated_ids = {p.user_id for p in losers}
    elif br.elimination_mode == "lives":
        eliminated_ids = {p.user_id for p in active if (p.lives or 0) <= 0}
    elif br.elimination_mode == "survival":
        cfg = br.elimination_config or {}
        eliminate_on_wrong = bool(cfg.get("eliminate_on_wrong", False))
        survival_window_ms = cfg.get("survival_window_ms")
        for p in active:
            attempt = this_q_attempts.get(p.user_id)
            if eliminate_on_wrong and (attempt is None or not attempt.is_correct):
                eliminated_ids.add(p.user_id)
                continue
            if (
                survival_window_ms is not None and attempt is not None
                and attempt.response_time_ms is not None and attempt.response_time_ms > survival_window_ms
            ):
                eliminated_ids.add(p.user_id)

    if eliminated_ids:
        _apply_eliminations(db, match, br, active, eliminated_ids, position, stats)

    still_active = [p for p in active if p.user_id not in eliminated_ids]
    n_before = len(active)
    br.remaining_players_count = len(still_active)
    db.add(br)
    db.commit()
    db.refresh(br)

    _check_phase_progression(db, match, br, still_active, previous_remaining_count=n_before)

    try:
        from app.core.competitive_ws import publish_competitive_event
        publish_competitive_event(str(match.id), {"type": "gameplay_update"})
    except Exception:
        logger.debug("battle royale gameplay_update publish failed match=%s", match.id, exc_info=True)


def _check_phase_progression(
    db: Session, match: CompetitiveMatch, br: CompetitiveBattleRoyaleMatch,
    still_active: List[CompetitiveMatchParticipant], *, previous_remaining_count: Optional[int] = None,
) -> None:
    """Task 4's phase cascade, reused by every elimination path (a normal
    per-question tick, and Task 6's disconnect-triggered elimination).
    Walks the BATTLE_ROYALE_PHASE_TRANSITIONS chain (running -> final_phase
    -> final_duel -> completed) sequentially within a single call so even a
    single elimination tick that drops the roster past BOTH the final_phase
    and final_duel thresholds at once (e.g. an aggressive score_elimination
    cut) still passes through each intermediate phase legally rather than
    attempting an invalid direct jump.

    `previous_remaining_count` (the active roster size right before THIS
    tick's eliminations were applied — passed by every caller, since each
    already computes it) drives Task 7's top_10/top_5 notifications: fired
    whenever the roster crosses exactly 10 or 5 remaining, independent of
    whatever final_phase_threshold this match is configured with (per spec,
    "fire top_10/top_5 whenever remaining_players_count crosses those exact
    numbers")."""
    if br.current_phase == "completed":
        return

    remaining = len(still_active)

    if previous_remaining_count is not None:
        if previous_remaining_count > 10 >= remaining:
            for p in still_active:
                emit(
                    db, event_type="battle_royale_top_10", user_id=p.user_id,
                    data={"match_id": str(match.id), "remaining_players_count": remaining},
                    dedup_key=f"battle_royale_top_10:{match.id}",
                )
        if previous_remaining_count > 5 >= remaining:
            for p in still_active:
                emit(
                    db, event_type="battle_royale_top_5", user_id=p.user_id,
                    data={"match_id": str(match.id), "remaining_players_count": remaining},
                    dedup_key=f"battle_royale_top_5:{match.id}",
                )

    if remaining <= 1:
        _finalize_battle_royale(db, match, br, still_active)
        return

    if br.current_phase == "running" and remaining <= br.final_phase_threshold:
        transition_phase(br, "final_phase")
        db.add(br)
        db.commit()
        db.refresh(br)
        event_log_service.log_event(
            db, match_id=match.id, actor_id=None, event_type="battle_royale_final_phase_started",
            meta={"remaining_players_count": remaining},
        )

    if remaining == 2 and br.current_phase == "final_phase":
        transition_phase(br, "final_duel")
        db.add(br)
        db.commit()
        db.refresh(br)
        event_log_service.log_event(
            db, match_id=match.id, actor_id=None, event_type="battle_royale_final_duel_started",
            meta={"remaining_players_count": remaining},
        )
        for p in still_active:
            emit(
                db, event_type="battle_royale_final_duel", user_id=p.user_id,
                data={"match_id": str(match.id)},
                dedup_key=f"battle_royale_final_duel:{match.id}",
            )


def _final_duel_key(
    p: CompetitiveMatchParticipant, stats: Dict[UUID, Dict[str, Any]], method: str, tie_break_rules: Dict[str, Any],
) -> Tuple[Any, ...]:
    """final_duel_method / tie_break_rules contract (Task 4):
      - 'total_score' (default): highest cumulative score wins; ties broken
        by tie_break_rules={"method": "fastest_response_time"|"accuracy"}
        (default "fastest_response_time" if the key is missing/unset).
      - 'fastest': highest score wins; ties are ALWAYS broken by average
        response time regardless of tie_break_rules — per spec, this method
        "only meaningfully differs from total_score in a tie".
      - 'sudden_death': V1 SIMPLIFICATION — falls back to 'total_score'. A
        genuine extra-question sudden-death round is architecture-ready
        (current_phase='final_duel' already exists as the hook point) but
        was not implemented in Part B given the added complexity of serving
        one more live question outside the match's pre-generated question
        list; see this phase's report.
      - 'combination': V1 SIMPLIFICATION — same fallback, same reason.
    """
    s = stats.get(p.user_id, {})
    tb_method = "fastest_response_time" if method == "fastest" else (tie_break_rules or {}).get("method", "fastest_response_time")
    if tb_method == "accuracy":
        secondary: Any = -s.get("accuracy_pct", 0.0)
    else:
        secondary = s.get("avg_response_time_ms") if s.get("avg_response_time_ms") is not None else float("inf")
    return (-p.score, secondary, s.get("wrong", 0), p.joined_at)


def _determine_final_duel_winner(
    db: Session, match: CompetitiveMatch, br: CompetitiveBattleRoyaleMatch, still_active: List[CompetitiveMatchParticipant],
) -> UUID:
    stats = bulk_attempt_stats(db, match.id)
    ranked = sorted(still_active, key=lambda p: _final_duel_key(p, stats, br.final_duel_method, br.tie_break_rules))
    return ranked[0].user_id


def _finalize_battle_royale(
    db: Session, match: CompetitiveMatch, br: CompetitiveBattleRoyaleMatch, still_active: List[CompetitiveMatchParticipant],
) -> None:
    """Match completion — called once remaining_players_count drops to <= 1
    (from process_elimination_tick / eliminate_participant_for_disconnect)
    OR once the question list is exhausted with >= 2 still active (from
    complete_from_exhausted_questions). Determines the winner, finalizes
    every participant's final_rank (1 = winner, ascending from there — the
    most-recently-eliminated participant already holds the best remaining
    number from _apply_eliminations, standard battle-royale convention),
    then reuses the EXISTING gameplay_service.finish_match(forced_results=...)
    pipeline (Phase 3/4/7/8/9 stats/replay/ranking/prediction/tournament
    hooks) for the actual match-completion side effects — Part B does not
    reimplement any of that."""
    if br.current_phase == "completed":
        return

    all_participants = match_service.get_participants(db, match.id)
    degenerate = False

    if len(still_active) == 1:
        winner_id: Optional[UUID] = still_active[0].user_id
    elif len(still_active) >= 2:
        winner_id = _determine_final_duel_winner(db, match, br, still_active)
    else:
        # Degenerate edge case: everyone active was eliminated in the SAME
        # tick (e.g. a simultaneous lives-mode double-KO). No sole survivor
        # exists to crown — fall back to whoever's provisional final_rank is
        # best among already-eliminated participants (i.e. eliminated most
        # recently / least-bad) so the match still completes cleanly rather
        # than getting stuck. Flagged in the completion event log.
        degenerate = True
        ranked_by_elim = sorted((p for p in all_participants if p.final_rank is not None), key=lambda p: p.final_rank)
        winner_id = ranked_by_elim[0].user_id if ranked_by_elim else (all_participants[0].user_id if all_participants else None)

    if still_active and winner_id is not None:
        stats = bulk_attempt_stats(db, match.id)
        others = [p for p in still_active if p.user_id != winner_id]
        others_ranked = sorted(others, key=lambda p: _final_duel_key(p, stats, br.final_duel_method, br.tie_break_rules))
        for idx, p in enumerate(others_ranked, start=2):
            p.final_rank = idx
            db.add(p)

    winner_participant = next((p for p in all_participants if p.user_id == winner_id), None) if winner_id else None
    if winner_participant is not None:
        winner_participant.final_rank = 1
        db.add(winner_participant)
    db.commit()

    transition_phase(br, "completed")
    db.add(br)
    db.commit()
    db.refresh(br)

    event_log_service.log_event(
        db, match_id=match.id, actor_id=None, event_type="battle_royale_completed",
        meta={
            "winner_id": str(winner_id) if winner_id else None, "final_duel_method": br.final_duel_method,
            "ranking_impact": br.ranking_impact, "remaining_at_completion": len(still_active),
            "degenerate_simultaneous_wipeout": degenerate,
        },
    )

    forced_results = {p.user_id: ("win" if p.user_id == winner_id else "loss") for p in all_participants}

    from app.services.competitive import gameplay_service  # lazy — avoids a module-load-time circular import
    gameplay_service.finish_match(db, match, forced_results=forced_results)

    # Task 7 — notification: match completion, to the winner at minimum
    # (every OTHER participant already got their own 'battle_royale_
    # eliminated' notification at the moment they personally went out).
    if winner_id is not None:
        emit(
            db, event_type="battle_royale_winner", user_id=winner_id,
            data={"match_id": str(match.id)},
            dedup_key=f"battle_royale_winner:{match.id}:{winner_id}",
        )

    # Task 2 — reward distribution / Task 3 — BR profile statistics: both
    # run right after the shared completion pipeline, each in its own
    # try/except (mirrors the defensive posture every other post-completion
    # side effect in this codebase already uses, e.g. gameplay_service.
    # finish_match's own Phase 8/Phase 9 hooks) — a bug in either must never
    # leave the match itself stuck uncompleted.
    try:
        distribute_battle_royale_rewards(db, match, br)
    except Exception:
        logger.exception("battle royale reward distribution failed match=%s", match.id)

    try:
        update_battle_royale_statistics(db, match, all_participants)
    except Exception:
        logger.exception("battle royale statistics update failed match=%s", match.id)


def complete_from_exhausted_questions(db: Session, match: CompetitiveMatch) -> None:
    """Called from gameplay_service.ensure_progressed() INSTEAD OF the
    generic finish_match(db, match) once a battle_royale match's question
    list runs out with >= 2 players still active (e.g. a lenient
    score_elimination config that never actually cuts the roster to 1) — the
    generic 2-player-or-draw completion logic doesn't know how to pick an
    N-player winner, so this applies final_duel_method via
    _finalize_battle_royale instead."""
    br = db.get(CompetitiveBattleRoyaleMatch, match.id)
    if br is None:
        from app.services.competitive import gameplay_service
        gameplay_service.finish_match(db, match)
        return
    if br.current_phase == "completed":
        return
    still_active = [p for p in match_service.get_participants(db, match.id) if p.elimination_round is None]
    _finalize_battle_royale(db, match, br, still_active)


def eliminate_participant_for_disconnect(
    db: Session, match: CompetitiveMatch, br: CompetitiveBattleRoyaleMatch, participant: CompetitiveMatchParticipant,
) -> None:
    """Task 6 — disconnect_policy='auto_eliminate' path, called by
    task_check_battle_royale_disconnect once the grace period elapses with
    no reconnection. Same elimination bookkeeping as a normal per-question
    elimination (_apply_eliminations) but for exactly ONE participant,
    outside the question-scoring tick, at whatever question_position is
    currently active — then cascades the same phase-progression/completion
    check a normal elimination would."""
    if participant.elimination_round is not None:
        return  # already eliminated by something else in the meantime

    position = match.current_question_position if match.current_question_position is not None else 0
    participants = match_service.get_participants(db, match.id)
    active = [p for p in participants if p.elimination_round is None]
    n_before = len(active)

    now = _now()
    participant.eliminated_at = now
    participant.elimination_round = position
    participant.final_rank = n_before  # sole elimination this instant — worst rank among currently-active
    db.add(participant)
    db.commit()

    event_log_service.log_event(
        db, match_id=match.id, actor_id=None, event_type="battle_royale_eliminated",
        meta={
            "user_id": str(participant.user_id), "final_rank": participant.final_rank,
            "question_position": position, "reason": "disconnect_auto_eliminate",
        },
    )
    try:
        from app.core.competitive_ws import publish_competitive_event
        publish_competitive_event(str(match.id), {
            "type": "battle_royale_eliminated", "user_id": str(participant.user_id),
            "final_rank": participant.final_rank, "question_position": position,
        })
    except Exception:
        logger.debug("battle royale disconnect-elimination WS push failed match=%s user=%s", match.id, participant.user_id, exc_info=True)

    emit(
        db, event_type="battle_royale_eliminated", user_id=participant.user_id,
        context={"rank": participant.final_rank},
        data={"match_id": str(match.id), "final_rank": participant.final_rank, "question_position": position},
        dedup_key=f"battle_royale_eliminated:{match.id}:{participant.user_id}",
    )

    still_active = [p for p in active if p.user_id != participant.user_id]
    br.remaining_players_count = len(still_active)
    db.add(br)
    db.commit()
    db.refresh(br)

    _check_phase_progression(db, match, br, still_active, previous_remaining_count=n_before)


# ─── Phase 10 — Battle Royale, Part C (completion side effects) ────────────
#
# Everything below is new in Part C. Both entry points are called exactly
# once, from _finalize_battle_royale right after gameplay_service.
# finish_match() returns (own try/except at each call site — see that
# function) — neither ever runs standalone.

def _apply_arena_rating_bonus(db: Session, *, user_id: UUID, amount: int) -> None:
    """arena_rating reward — a flat bonus applied ON TOP of whatever Task
    1's pairwise-average Elo delta already produced for this match, never a
    re-run of that calculation. Verbatim copy of tournament_service.
    _apply_arena_rating_bonus's exact logic (clamp at competitive_mmr_floor,
    re-derive league_id/rank_tier/highest_rating) — kept as its own local
    copy rather than a cross-import so Part C's reward path never needs to
    depend on tournament_service, mirroring how battle_royale_service
    already never imports tournament_service anywhere else."""
    from app.models.admin import PlatformSettings
    from app.services.competitive import statistics_service

    stats = db.get(CompetitiveStatistics, user_id) or statistics_service.get_or_create_statistics(db, user_id)
    settings_row = db.get(PlatformSettings, True) or PlatformSettings()
    new_rating = max(settings_row.competitive_mmr_floor, stats.rating + amount)
    stats.rating = new_rating

    from app.services.competitive import ranking_service
    league = ranking_service.get_league_for_rating(db, new_rating)
    stats.league_id = league.id if league else None
    stats.rank_tier = ranking_service.league_slug(league)
    if new_rating > stats.highest_rating:
        stats.highest_rating = new_rating
        if league is not None:
            stats.best_rank_tier = ranking_service.league_slug(league)
    stats.updated_at = _now()
    db.add(stats)
    db.commit()


def _grant_battle_royale_reward(
    db: Session, *, match: CompetitiveMatch, user_id: UUID, reward: CompetitiveBattleRoyaleReward,
) -> None:
    """Task 2 — grants exactly one configured reward row to one participant.
    Reuses the EXACT SAME granting primitives Phase 7/9 already established
    (reward_service.grant_reward for ep/badge/sticker/title/frame — live for
    ep/badge, recorded_only for the rest; arena_xp via the Phase 8
    competitive_spectator_stats.arena_xp counter, exactly like Phase 9's
    tournament rewards; arena_rating via the same flat-bonus-on-top pattern
    as tournament_service._apply_arena_rating_bonus; season_points recorded-
    only — Phase 9 Part B already established there is no dedicated "season
    points" ledger separate from competitive_season_stats.mmr, which every
    real match already updates independently, so battle_royale rewards
    follow the exact same precedent rather than inventing a new ledger)."""
    from app.services.competitive import reward_service, spectator_service

    rtype = reward.reward_type
    try:
        if rtype in ("ep", "badge", "sticker", "title", "frame"):
            reward_service.grant_reward(
                db, user_id=user_id, season_id=None, source="battle_royale", reward_type=rtype,
                reward_ref=reward.reward_ref, reward_amount=reward.reward_amount,
                battle_royale_match_id=match.id,
            )
            return
        elif rtype == "arena_rating":
            _apply_arena_rating_bonus(db, user_id=user_id, amount=reward.reward_amount or 0)
            status_ = "granted"
        elif rtype == "arena_xp":
            spec_stats = spectator_service.get_or_create_spectator_stats(db, user_id)
            spec_stats.arena_xp += reward.reward_amount or 0
            spec_stats.updated_at = _now()
            db.add(spec_stats)
            db.commit()
            status_ = "granted"
        else:  # season_points and anything else not natively applied
            status_ = "recorded_only"
    except Exception:
        logger.exception(
            "battle_royale_service._grant_battle_royale_reward: applying reward_type=%s failed for user=%s match=%s",
            rtype, user_id, match.id,
        )
        status_ = "recorded_only"

    grant = CompetitiveRewardGrant(
        user_id=user_id, season_id=None, source="battle_royale", reward_type=rtype,
        reward_ref=reward.reward_ref, reward_amount=reward.reward_amount, status=status_,
        battle_royale_match_id=match.id,
    )
    db.add(grant)
    db.commit()
    db.refresh(grant)
    emit(
        db, event_type="competitive_reward_received", user_id=user_id,
        data={
            "reward_type": rtype, "reward_ref": reward.reward_ref,
            "reward_amount": reward.reward_amount, "source": "battle_royale", "match_id": str(match.id),
        },
        dedup_key=f"competitive_reward_received:{grant.id}",
    )


def distribute_battle_royale_rewards(db: Session, match: CompetitiveMatch, br: CompetitiveBattleRoyaleMatch) -> int:
    """Task 2 — called once, right after finish_match() returns. Every
    configured CompetitiveBattleRoyaleReward row on this match is matched
    against every participant whose final_rank falls within
    [rank_min, rank_max] and granted."""
    rewards = list_rewards(db, match.id)
    if not rewards:
        return 0

    participants = match_service.get_participants(db, match.id)
    granted = 0
    for reward in rewards:
        targets = [
            p for p in participants
            if p.final_rank is not None and reward.rank_min <= p.final_rank <= reward.rank_max
        ]
        for participant in targets:
            _grant_battle_royale_reward(db, match=match, user_id=participant.user_id, reward=reward)
            granted += 1
    return granted


def update_battle_royale_statistics(
    db: Session, match: CompetitiveMatch, participants: List[CompetitiveMatchParticipant],
) -> None:
    """Task 3 — called once, right after finish_match() returns. Populates/
    updates CompetitiveBattleRoyaleStatistics for every participant of this
    match. `participants` must already have final_rank (1..N) populated by
    _finalize_battle_royale before this runs."""
    from app.models.competitive import CompetitiveBattleRoyaleStatistics, CompetitiveMatchPlayerStats

    for p in participants:
        stats = db.get(CompetitiveBattleRoyaleStatistics, p.user_id)
        if stats is None:
            stats = CompetitiveBattleRoyaleStatistics(user_id=p.user_id)

        stats.matches_played += 1
        if p.final_rank == 1:
            stats.wins += 1
        if p.final_rank is not None and p.final_rank <= 3:
            stats.top3_finishes += 1
        if p.final_rank is not None and p.final_rank <= 10:
            stats.top10_finishes += 1
        if p.final_rank is not None:
            stats.best_rank = p.final_rank if stats.best_rank is None else min(stats.best_rank, p.final_rank)

        # Furthest question_position reached before elimination — a
        # participant with no elimination_round survived to the very end
        # (winner or final-duel runner-up), so their survival position is
        # the full match length.
        survival_position = p.elimination_round if p.elimination_round is not None else match.question_count
        stats.longest_survival_position = (
            survival_position if stats.longest_survival_position is None
            else max(stats.longest_survival_position, survival_position)
        )

        player_stat_row = db.get(CompetitiveMatchPlayerStats, (match.id, p.user_id))
        match_accuracy_pct = player_stat_row.accuracy_pct if player_stat_row is not None else 0.0
        stats.total_accuracy_pct_sum = (stats.total_accuracy_pct_sum or 0) + match_accuracy_pct

        # favorite_subject_id/favorite_difficulty — increment-a-counter,
        # take-the-max, same style as CompetitiveStatistics.subject_
        # breakdown/difficulty_breakdown's own per-attempt breakdowns (see
        # migration 087's header comment).
        subject_counts = dict(stats.subject_counts or {})
        for sid in (match.subject_ids or []):
            key = str(sid)
            subject_counts[key] = subject_counts.get(key, 0) + 1
        if subject_counts:
            stats.subject_counts = subject_counts
            best_subject_key = max(subject_counts.items(), key=lambda kv: kv[1])[0]
            stats.favorite_subject_id = UUID(best_subject_key)

        if match.difficulty:
            difficulty_counts = dict(stats.difficulty_counts or {})
            difficulty_counts[match.difficulty] = difficulty_counts.get(match.difficulty, 0) + 1
            stats.difficulty_counts = difficulty_counts
            stats.favorite_difficulty = max(difficulty_counts.items(), key=lambda kv: kv[1])[0]

        stats.updated_at = _now()
        db.add(stats)
    db.commit()

    # Phase 15 — Mission Engine hooks (BR has its own completion pipeline,
    # separate from the 2-player finish_match's mission hooks).
    from app.services.competitive import mission_service
    for p in participants:
        mission_service.record_event(db, user_id=p.user_id, metric_key="arena_battle_royale_played", amount=1)
        if p.final_rank == 1:
            mission_service.record_event(db, user_id=p.user_id, metric_key="arena_br_win", amount=1)
        if p.final_rank is not None and p.final_rank <= 10:
            mission_service.record_event(db, user_id=p.user_id, metric_key="arena_br_top10", amount=1)


# ─── Phase 10 — Battle Royale, Part C (player-facing read APIs) ────────────

def get_full_leaderboard(
    db: Session, match: CompetitiveMatch, *, question_position: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Task 4 — full standings, the complement to the live WS push's capped
    top-N (gameplay_service._battle_royale_leaderboard's _BR_LEADERBOARD_TOP_N).
    - Completed match: final standings, ordered by final_rank (1..N).
    - Live match: the latest CompetitiveBattleRoyaleRankingSnapshot at or
      before `question_position` (or the latest snapshot overall if
      question_position is None) — falls back to a live rank_key()
      computation if no snapshot has been written yet (e.g. queried before
      question 0 has been revealed)."""
    participants = match_service.get_participants(db, match.id)
    profiles = {
        pr.id: pr for pr in db.exec(select(Profile).where(Profile.id.in_({p.user_id for p in participants}))).all()
    } if participants else {}
    eliminated_by_user = {p.user_id: p.elimination_round is not None for p in participants}

    def _entry(*, rank: int, user_id: UUID, score: int) -> Dict[str, Any]:
        profile = profiles.get(user_id)
        return {
            "rank": rank, "user_id": user_id,
            "full_name": profile.full_name if profile else None,
            "avatar_url": profile.avatar_url if profile else None,
            "score": score, "is_eliminated": eliminated_by_user.get(user_id, False),
        }

    if match.status == "completed":
        ranked = sorted(participants, key=lambda p: p.final_rank if p.final_rank is not None else 10**9)
        return [_entry(rank=p.final_rank or (idx + len(participants)), user_id=p.user_id, score=p.score) for idx, p in enumerate(ranked)]

    query = select(CompetitiveBattleRoyaleRankingSnapshot).where(CompetitiveBattleRoyaleRankingSnapshot.match_id == match.id)
    if question_position is not None:
        query = query.where(CompetitiveBattleRoyaleRankingSnapshot.question_position <= question_position)
    snapshots = list(db.exec(query.order_by(CompetitiveBattleRoyaleRankingSnapshot.question_position.desc())).all())

    if not snapshots:
        stats = bulk_attempt_stats(db, match.id)
        ranked = sorted(participants, key=lambda p: rank_key(p, stats))
        return [_entry(rank=idx, user_id=p.user_id, score=p.score) for idx, p in enumerate(ranked, start=1)]

    latest_position = snapshots[0].question_position
    latest = sorted((s for s in snapshots if s.question_position == latest_position), key=lambda s: s.rank)
    return [_entry(rank=s.rank, user_id=s.user_id, score=s.score) for s in latest]


def get_or_create_battle_royale_statistics(db: Session, user_id: UUID) -> "CompetitiveBattleRoyaleStatistics":
    """Task 4 — mirrors statistics_service.get_or_create_statistics's exact
    create-on-read pattern."""
    from app.models.competitive import CompetitiveBattleRoyaleStatistics

    stats = db.get(CompetitiveBattleRoyaleStatistics, user_id)
    if stats is not None:
        return stats
    stats = CompetitiveBattleRoyaleStatistics(user_id=user_id)
    db.add(stats)
    db.commit()
    db.refresh(stats)
    return stats


def get_user_battle_royale_history(db: Session, user_id: UUID) -> Dict[str, Any]:
    """Task 4 — mirrors tournament_service.get_user_tournament_history's
    exact shape/style: every Battle Royale this user participated in, their
    final placement, and every battle_royale-sourced reward grant
    (competitive_reward_grants, source='battle_royale' — the same unified
    'my rewards' ledger every other reward source already writes into)."""
    from app.models.competitive import CompetitiveMatchPlayerStats

    participant_rows = list(
        db.exec(
            select(CompetitiveMatchParticipant)
            .join(CompetitiveBattleRoyaleMatch, CompetitiveBattleRoyaleMatch.match_id == CompetitiveMatchParticipant.match_id)
            .where(CompetitiveMatchParticipant.user_id == user_id)
            .order_by(CompetitiveMatchParticipant.joined_at.desc())
        ).all()
    )

    matches_out: List[Dict[str, Any]] = []
    if participant_rows:
        match_ids = {r.match_id for r in participant_rows}
        matches = {m.id: m for m in db.exec(select(CompetitiveMatch).where(CompetitiveMatch.id.in_(match_ids))).all()}
        player_stats_by_match = {
            ps.match_id: ps
            for ps in db.exec(
                select(CompetitiveMatchPlayerStats)
                .where(CompetitiveMatchPlayerStats.match_id.in_(match_ids))
                .where(CompetitiveMatchPlayerStats.user_id == user_id)
            ).all()
        }
        for r in participant_rows:
            m = matches.get(r.match_id)
            if m is None:
                continue
            ps = player_stats_by_match.get(r.match_id)
            matches_out.append({
                "match_id": m.id, "status": m.status, "subject_ids": m.subject_ids, "difficulty": m.difficulty,
                "final_rank": r.final_rank,
                "questions_answered": (ps.correct_count + ps.wrong_count) if ps is not None else None,
                "score": r.score, "joined_at": r.joined_at,
            })

    grants = list(
        db.exec(
            select(CompetitiveRewardGrant)
            .where(CompetitiveRewardGrant.user_id == user_id)
            .where(CompetitiveRewardGrant.source == "battle_royale")
            .order_by(CompetitiveRewardGrant.granted_at.desc())
        ).all()
    )
    rewards_out = [
        {
            "match_id": g.battle_royale_match_id, "reward_type": g.reward_type, "reward_ref": g.reward_ref,
            "reward_amount": g.reward_amount, "status": g.status, "granted_at": g.granted_at,
        }
        for g in grants
    ]
    return {"matches": matches_out, "rewards": rewards_out}


# ─── Phase 10 — Battle Royale, Part C (admin live-match actions) ───────────

def admin_kick_participant(
    db: Session, match: CompetitiveMatch, br: CompetitiveBattleRoyaleMatch, *, user_id: UUID, actor_email: Optional[str] = None,
) -> None:
    """Task 6 — remove a player from an active lobby BEFORE the match
    starts. Different from disqualify (no elimination bookkeeping needed —
    the player was never actually playing yet). Only allowed while
    current_phase='waiting_room', mirroring leave_battle_royale's own
    cutoff; the only difference from a voluntary leave is who triggers it
    and the event_type logged."""
    if br.current_phase != "waiting_room":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Impossible d'exclure un joueur une fois le compte à rebours lancé — utilise /disqualify une fois la partie en cours.",
        )
    participant = db.exec(
        select(CompetitiveMatchParticipant)
        .where(CompetitiveMatchParticipant.match_id == match.id)
        .where(CompetitiveMatchParticipant.user_id == user_id)
        .where(CompetitiveMatchParticipant.left_at.is_(None))
    ).first()
    if participant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ce joueur ne participe pas à ce Battle Royale.")

    participant.left_at = _now()
    db.add(participant)
    db.commit()

    br.remaining_players_count = max(0, (br.remaining_players_count or 1) - 1)
    br.updated_at = _now()
    db.add(br)
    db.commit()

    event_log_service.log_event(
        db, match_id=match.id, actor_id=None, event_type="battle_royale_kicked",
        meta={"user_id": str(user_id), "admin_email": actor_email, "remaining_players_count": br.remaining_players_count},
    )


def admin_disqualify_participant(
    db: Session, match: CompetitiveMatch, br: CompetitiveBattleRoyaleMatch, *, user_id: UUID,
    reason: Optional[str] = None, actor_email: Optional[str] = None,
) -> None:
    """Task 6 — for a match already running/final_phase/final_duel: same
    elimination bookkeeping as a normal per-question elimination, reusing
    _apply_eliminations VERBATIM (eliminated_at/elimination_round/
    provisional final_rank/WS signal/notification), then cascades the same
    phase-progression/completion check any other elimination triggers."""
    if br.current_phase not in ("running", "final_phase", "final_duel"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cette action nécessite un Battle Royale en cours.")

    participants = match_service.get_participants(db, match.id)
    participant = next((p for p in participants if p.user_id == user_id), None)
    if participant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ce joueur ne participe pas à ce Battle Royale.")
    if participant.elimination_round is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce joueur est déjà éliminé.")

    active = [p for p in participants if p.elimination_round is None]
    n_before = len(active)
    position = match.current_question_position if match.current_question_position is not None else 0
    stats = bulk_attempt_stats(db, match.id)

    _apply_eliminations(db, match, br, active, {user_id}, position, stats)

    event_log_service.log_event(
        db, match_id=match.id, actor_id=None, event_type="battle_royale_admin_disqualified",
        meta={"user_id": str(user_id), "reason": reason, "admin_email": actor_email},
    )

    still_active = [p for p in active if p.user_id != user_id]
    br.remaining_players_count = len(still_active)
    db.add(br)
    db.commit()
    db.refresh(br)

    _check_phase_progression(db, match, br, still_active, previous_remaining_count=n_before)


def admin_restart_battle_royale(
    db: Session, match: CompetitiveMatch, br: CompetitiveBattleRoyaleMatch, *, actor_email: Optional[str] = None,
) -> CompetitiveMatch:
    """Task 6 — genuinely different shape than Phase 9's tournament match
    restart: there is no bracket slot to reset (a Battle Royale IS the
    whole match, not one match inside a bracket). Simplest correct
    behavior, per spec: cancel the current match/config outright; the admin
    creates a fresh Battle Royale via the existing POST /battle-royales
    endpoint. Automatic re-creation (same config, fresh roster) is
    deliberately NOT built here — a conservative scope choice, not an
    oversight (see this phase's report)."""
    match = cancel_battle_royale(
        db, match, br, actor_email=actor_email,
        reason="Battle Royale redémarré par un administrateur — un nouveau Battle Royale doit être créé pour relancer.",
    )
    event_log_service.log_event(
        db, match_id=match.id, actor_id=None, event_type="battle_royale_restarted",
        meta={"admin_email": actor_email},
    )
    return match


def get_admin_monitor(db: Session, match: CompetitiveMatch, br: CompetitiveBattleRoyaleMatch) -> Dict[str, Any]:
    """Task 6 — live admin monitoring view: current phase, remaining count,
    full leaderboard snapshot, recent elimination events, spectator count
    (reuses Phase 8's spectator_presence_service, exactly like every other
    live-match admin view already does)."""
    from app.services.competitive import spectator_presence_service

    leaderboard = get_full_leaderboard(db, match)
    recent_eliminations = [
        {
            "user_id": e.meta.get("user_id"), "final_rank": e.meta.get("final_rank"),
            "question_position": e.meta.get("question_position"), "reason": e.meta.get("reason"),
            "at": e.created_at.isoformat(),
        }
        for e in event_log_service.list_events(db, match.id)  # already created_at DESC
        if e.event_type == "battle_royale_eliminated"
    ][:20]
    spectator_count = 0
    try:
        spectator_count = spectator_presence_service.get_count(match.id)
    except Exception:
        logger.debug("battle royale admin monitor: spectator count lookup failed match=%s", match.id, exc_info=True)

    return {
        "match_id": match.id, "match_status": match.status, "current_phase": br.current_phase,
        "remaining_players_count": br.remaining_players_count, "question_count": match.question_count,
        "current_question_position": match.current_question_position,
        "leaderboard": leaderboard, "recent_eliminations": recent_eliminations,
        "spectator_count": spectator_count,
    }


def get_battle_royales_analytics(db: Session) -> Dict[str, Any]:
    """Task 6 — platform-wide (NOT scoped to one match), mirrors
    tournament_service.get_tournaments_analytics's exact style/shape."""
    from collections import Counter, defaultdict

    from app.models.competitive import CompetitiveSpectator

    matches = list(db.exec(select(CompetitiveMatch).where(CompetitiveMatch.match_type == "battle_royale")).all())
    completed = [m for m in matches if m.status == "completed"]
    match_ids = [m.id for m in matches]

    durations = [
        (m.completed_at - m.started_at).total_seconds() for m in completed if m.started_at and m.completed_at
    ]

    participants_all = (
        list(db.exec(select(CompetitiveMatchParticipant).where(CompetitiveMatchParticipant.match_id.in_(match_ids))).all())
        if match_ids else []
    )
    count_by_match: Dict[UUID, int] = defaultdict(int)
    for p in participants_all:
        count_by_match[p.match_id] += 1
    per_match_counts = list(count_by_match.values())
    avg_players = round(sum(per_match_counts) / len(per_match_counts), 2) if per_match_counts else 0.0
    peak_players = max(per_match_counts, default=0)

    attempts = (
        list(
            db.exec(
                select(CompetitiveQuestionAttempt)
                .join(CompetitiveMatchQuestion, CompetitiveMatchQuestion.id == CompetitiveQuestionAttempt.match_question_id)
                .where(CompetitiveMatchQuestion.match_id.in_(match_ids))
            ).all()
        )
        if match_ids else []
    )
    correct = sum(1 for a in attempts if a.is_correct)

    # Dropout rate: disconnected-and-never-reconnected vs total participants
    # — approximated from every logged 'battle_royale_eliminated' event
    # whose meta carries reason='disconnect_auto_eliminate' (see
    # eliminate_participant_for_disconnect) against every participant ever
    # registered across every Battle Royale.
    disconnect_eliminations = 0
    for m in matches:
        for e in event_log_service.list_events(db, m.id):
            if e.event_type == "battle_royale_eliminated" and e.meta.get("reason") == "disconnect_auto_eliminate":
                disconnect_eliminations += 1
    total_participants = len(participants_all)
    dropout_rate_pct = round((disconnect_eliminations / total_participants) * 100, 2) if total_participants else 0.0

    subject_counter: Counter = Counter()
    for m in matches:
        for sid in (m.subject_ids or []):
            subject_counter[str(sid)] += 1
    top_subjects = subject_counter.most_common(5)

    spectators_total = (
        len(list(db.exec(select(CompetitiveSpectator.id).where(CompetitiveSpectator.match_id.in_(match_ids))).all()))
        if match_ids else 0
    )
    rewards_distributed = len(
        list(db.exec(select(CompetitiveRewardGrant.id).where(CompetitiveRewardGrant.source == "battle_royale")).all())
    )

    return {
        "total_matches": len(matches), "completed_matches": len(completed),
        "avg_duration_sec": round(sum(durations) / len(durations)) if durations else None,
        "avg_players_per_match": avg_players, "peak_players_single_match": peak_players,
        "questions_answered": len(attempts),
        "accuracy_pct": round((correct / len(attempts)) * 100, 2) if attempts else 0.0,
        "dropout_rate_pct": dropout_rate_pct,
        "total_spectators": spectators_total,
        "rewards_distributed": rewards_distributed,
        "top_subject_ids": [{"subject_id": s, "match_count": c} for s, c in top_subjects],
    }
