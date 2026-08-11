from __future__ import annotations

"""
Tournament Manager — Competitive Arena Phase 9, Part A (Foundation).

Owns the tournament status machine (mirrors match_service.py's TRANSITIONS
style), registration/waiting-list bookkeeping, and Single Elimination
bracket generation + seeding. Routers stay thin and call into this service,
exactly like every other Phase 1-8 router does.

Tournament matches are structurally 2-player duels underneath — they reuse
the existing CompetitiveMatch / Arena Match Engine verbatim. This module
never touches gameplay logic; it only manages the bracket TREE
(competitive_tournament_matches) that points at a CompetitiveMatch once one
exists for a given slot.

Explicitly NOT built here (left for Part B — see this phase's final
report): actually creating the underlying CompetitiveMatch duel for a
'ready' bracket slot, advancing a winner into the next round when a REAL
(non-bye) match completes, reward distribution execution, tournament
WebSocket, disqualify/replace/restart admin actions, tournament statistics/
history endpoints, and wiring gameplay_service.finish_match to call back
into this module.
"""

import logging
import random
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlmodel import Session, func, select

from app.core.tournament_ws import publish_tournament_event
from app.models.catalog import Level
from app.models.competitive import (
    CompetitiveMatch,
    CompetitiveMatchParticipant,
    CompetitiveRewardGrant,
    CompetitiveStatistics,
    CompetitiveTournament,
    CompetitiveTournamentLog,
    CompetitiveTournamentMatch,
    CompetitiveTournamentParticipant,
    CompetitiveTournamentReward,
    CompetitiveTournamentRound,
)
from app.models.profile import Profile, StudentProfile
from app.services.competitive import match_service
from app.services.notification_engine import emit

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─── Tournament-level audit log ──────────────────────────────────────────────

def log_event(
    db: Session, *, tournament_id: UUID, actor_id: Optional[UUID], event_type: str,
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    """Never raises — logging a tournament's history must never break the
    action that triggered it (same defensive posture as event_log_service /
    notification_engine.emit)."""
    try:
        db.add(CompetitiveTournamentLog(tournament_id=tournament_id, actor_id=actor_id, event_type=event_type, meta=meta or {}))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.exception("tournament log_event failed tournament_id=%s event_type=%s", tournament_id, event_type)


# ─── Status machine ──────────────────────────────────────────────────────────

#: Terminal statuses map to an empty set — nothing may leave them.
TRANSITIONS: Dict[str, set] = {
    "draft": {"published", "cancelled"},
    "published": {"registration_open", "cancelled"},
    "registration_open": {"registration_closed", "cancelled"},
    "registration_closed": {"bracket_generated", "cancelled"},
    "bracket_generated": {"running", "cancelled"},
    "running": {"paused", "completed", "cancelled"},
    "paused": {"running", "cancelled"},
    "completed": set(),
    "cancelled": set(),
    "archived": set(),
}


def transition(tournament: CompetitiveTournament, new_status: str) -> CompetitiveTournament:
    allowed = TRANSITIONS.get(tournament.status, set())
    if new_status not in allowed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Transition invalide : {tournament.status} -> {new_status}",
        )
    tournament.status = new_status
    tournament.updated_at = _now()
    return tournament


def get_tournament_or_404(db: Session, tournament_id: UUID) -> CompetitiveTournament:
    tournament = db.get(CompetitiveTournament, tournament_id)
    if tournament is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tournoi introuvable.")
    return tournament


def list_tournaments_admin(db: Session, *, status_filter: Optional[str] = None) -> List[CompetitiveTournament]:
    query = select(CompetitiveTournament)
    if status_filter:
        query = query.where(CompetitiveTournament.status == status_filter)
    query = query.order_by(CompetitiveTournament.created_at.desc())
    return list(db.exec(query).all())


def list_tournaments_public(
    db: Session, *, status_filter: Optional[str] = None, subject_id: Optional[UUID] = None,
    school_level_id: Optional[UUID] = None, difficulty: Optional[str] = None,
    tournament_type: Optional[str] = None,
) -> List[CompetitiveTournament]:
    """Never shows 'draft' tournaments — player-facing list only, simplest
    correct behaviour (see Phase 9 Part A report)."""
    query = select(CompetitiveTournament).where(CompetitiveTournament.status != "draft")
    if status_filter:
        query = query.where(CompetitiveTournament.status == status_filter)
    if subject_id:
        query = query.where(CompetitiveTournament.subject_id == subject_id)
    if school_level_id:
        query = query.where(CompetitiveTournament.school_level_id == school_level_id)
    if difficulty:
        query = query.where(CompetitiveTournament.difficulty == difficulty)
    if tournament_type:
        query = query.where(CompetitiveTournament.tournament_type == tournament_type)
    query = query.order_by(CompetitiveTournament.starts_at.asc())
    return list(db.exec(query).all())


def _validate_schedule(registration_opens_at: datetime, registration_closes_at: datetime, starts_at: datetime) -> None:
    if registration_closes_at <= registration_opens_at:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="registration_closes_at doit être après registration_opens_at.")
    if starts_at < registration_closes_at:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="starts_at doit être après (ou égal à) registration_closes_at.")


def create_tournament(db: Session, *, organizer_id: Optional[UUID] = None, actor_email: Optional[str] = None, **fields) -> CompetitiveTournament:
    """organizer_id is a profiles.id FK per spec ("the admin who created
    it"), but admins are never profiles rows in this codebase (see
    app/dependencies.py's get_admin_user — its own comment: admin `id` is
    ALWAYS None, deliberately never written into a profiles.id FK). So
    organizer_id stays NULL for every admin-created tournament; the acting
    admin's email is recorded in the creation log entry instead, mirroring
    the exact admin_cancelled/admin_email pattern already used elsewhere
    in this router (e.g. admin_cancel_match)."""
    _validate_schedule(fields["registration_opens_at"], fields["registration_closes_at"], fields["starts_at"])
    tournament = CompetitiveTournament(organizer_id=organizer_id, status="draft", **fields)
    db.add(tournament)
    db.commit()
    db.refresh(tournament)
    log_event(db, tournament_id=tournament.id, actor_id=None, event_type="tournament_created", meta={"name": tournament.name, "admin_email": actor_email})
    return tournament


def update_tournament(db: Session, tournament_id: UUID, *, actor_email: Optional[str] = None, **fields) -> CompetitiveTournament:
    """Only allowed while draft/published — anything past registration_open
    risks invalidating already-registered players' expectations (bracket
    size, schedule). Reject with 409 otherwise (deliberate cutoff — a
    simpler/more conservative choice than allowing edits mid-registration)."""
    tournament = get_tournament_or_404(db, tournament_id)
    if tournament.status not in ("draft", "published"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce tournoi ne peut plus être modifié (inscriptions déjà entamées ou tournoi terminé).")

    updates = {k: v for k, v in fields.items() if v is not None}
    new_opens = updates.get("registration_opens_at", tournament.registration_opens_at)
    new_closes = updates.get("registration_closes_at", tournament.registration_closes_at)
    new_starts = updates.get("starts_at", tournament.starts_at)
    if any(k in updates for k in ("registration_opens_at", "registration_closes_at", "starts_at")):
        _validate_schedule(new_opens, new_closes, new_starts)

    for key, value in updates.items():
        setattr(tournament, key, value)
    tournament.updated_at = _now()
    db.add(tournament)
    db.commit()
    db.refresh(tournament)
    log_event(db, tournament_id=tournament.id, actor_id=None, event_type="tournament_updated", meta={"fields": list(updates.keys()), "admin_email": actor_email})
    return tournament


def publish_tournament(db: Session, tournament_id: UUID, *, actor_email: Optional[str] = None) -> CompetitiveTournament:
    tournament = get_tournament_or_404(db, tournament_id)
    transition(tournament, "published")
    db.add(tournament)
    db.commit()
    db.refresh(tournament)
    log_event(db, tournament_id=tournament.id, actor_id=None, event_type="tournament_published", meta={"admin_email": actor_email})
    return tournament


def open_registration(db: Session, tournament_id: UUID, *, actor_email: Optional[str] = None) -> CompetitiveTournament:
    """Admin-triggerable at any time from 'published' — not gated on
    now() >= registration_opens_at, so an admin retains manual control
    (e.g. opening early). registration_opens_at stays purely informational/
    display metadata for the countdown UI."""
    tournament = get_tournament_or_404(db, tournament_id)
    transition(tournament, "registration_open")
    db.add(tournament)
    db.commit()
    db.refresh(tournament)
    log_event(db, tournament_id=tournament.id, actor_id=None, event_type="tournament_registration_opened", meta={"admin_email": actor_email})
    _notify_registration_opened(db, tournament)
    return tournament


def _notify_registration_opened(db: Session, tournament: CompetitiveTournament) -> None:
    """Broad-audience notification for a newly-opened tournament. This
    codebase's only existing precedent for "notify a whole audience at once"
    is season_service._notify_season_started, which loops over every user
    with a CompetitiveStatistics row (i.e. anyone who's ever engaged with
    the Arena) — there is no "users eligible for tournament X" query builder
    anywhere else to reuse instead, so this mirrors that exact precedent
    rather than inventing a new targeting mechanism. Known limitation: not
    filtered further by this tournament's subject/school_level eligibility
    (see Phase 9 Part B report)."""
    try:
        user_ids = list(db.exec(select(CompetitiveStatistics.user_id)).all())
        for user_id in user_ids:
            emit(
                db, event_type="tournament_registration_opened", user_id=user_id,
                context={"tournament_name": tournament.name},
                data={"tournament_id": str(tournament.id)},
                dedup_key=f"tournament_registration_opened:{tournament.id}:{user_id}",
            )
    except Exception:
        logger.exception("tournament_registration_opened broadcast failed tournament=%s", tournament.id)


def close_registration(db: Session, tournament_id: UUID, *, actor_email: Optional[str] = None) -> CompetitiveTournament:
    tournament = get_tournament_or_404(db, tournament_id)
    transition(tournament, "registration_closed")
    db.add(tournament)
    db.commit()
    db.refresh(tournament)
    log_event(db, tournament_id=tournament.id, actor_id=None, event_type="tournament_registration_closed", meta={"admin_email": actor_email})
    return tournament


def cancel_tournament(db: Session, tournament_id: UUID, *, actor_email: Optional[str] = None, reason: Optional[str] = None) -> CompetitiveTournament:
    """Cascade-cancels any competitive_matches rows tied to this tournament
    that haven't started yet."""
    tournament = get_tournament_or_404(db, tournament_id)
    transition(tournament, "cancelled")
    db.add(tournament)
    db.commit()

    bracket_matches = list(
        db.exec(select(CompetitiveTournamentMatch).where(CompetitiveTournamentMatch.tournament_id == tournament_id)).all()
    )
    cancelled_duels = 0
    for bm in bracket_matches:
        if bm.match_id is None:
            continue
        duel = db.get(CompetitiveMatch, bm.match_id)
        if duel is None or duel.status in ("completed", "cancelled", "expired", "abandoned"):
            continue
        match_service.transition(duel, "cancelled")
        duel.cancelled_at = _now()
        duel.cancelled_reason = reason or "Tournoi annulé par un administrateur."
        db.add(duel)
        cancelled_duels += 1
    db.commit()
    db.refresh(tournament)

    log_event(db, tournament_id=tournament.id, actor_id=None, event_type="tournament_cancelled", meta={"reason": reason, "duels_cancelled": cancelled_duels, "admin_email": actor_email})
    return tournament


def pause_tournament(db: Session, tournament_id: UUID, *, actor_email: Optional[str] = None) -> CompetitiveTournament:
    tournament = get_tournament_or_404(db, tournament_id)
    transition(tournament, "paused")
    db.add(tournament)
    db.commit()
    db.refresh(tournament)
    log_event(db, tournament_id=tournament.id, actor_id=None, event_type="tournament_paused", meta={"admin_email": actor_email})
    return tournament


def resume_tournament(db: Session, tournament_id: UUID, *, actor_email: Optional[str] = None) -> CompetitiveTournament:
    tournament = get_tournament_or_404(db, tournament_id)
    transition(tournament, "running")
    db.add(tournament)
    db.commit()
    db.refresh(tournament)
    log_event(db, tournament_id=tournament.id, actor_id=None, event_type="tournament_resumed", meta={"admin_email": actor_email})
    return tournament


# ─── Reward config CRUD ──────────────────────────────────────────────────────

def list_rewards(db: Session, tournament_id: UUID) -> List[CompetitiveTournamentReward]:
    return list(
        db.exec(select(CompetitiveTournamentReward).where(CompetitiveTournamentReward.tournament_id == tournament_id)).all()
    )


def create_reward(
    db: Session, tournament_id: UUID, *, actor_email: Optional[str] = None, placement: str, reward_type: str,
    reward_ref: Optional[str], reward_amount: Optional[int],
) -> CompetitiveTournamentReward:
    get_tournament_or_404(db, tournament_id)
    reward = CompetitiveTournamentReward(
        tournament_id=tournament_id, placement=placement, reward_type=reward_type,
        reward_ref=reward_ref, reward_amount=reward_amount,
    )
    db.add(reward)
    db.commit()
    db.refresh(reward)
    log_event(db, tournament_id=tournament_id, actor_id=None, event_type="tournament_reward_created", meta={"placement": placement, "reward_type": reward_type, "admin_email": actor_email})
    return reward


def delete_reward(db: Session, tournament_id: UUID, reward_id: UUID, *, actor_email: Optional[str] = None) -> None:
    reward = db.get(CompetitiveTournamentReward, reward_id)
    if reward is None or reward.tournament_id != tournament_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Récompense introuvable pour ce tournoi.")
    db.delete(reward)
    db.commit()
    log_event(db, tournament_id=tournament_id, actor_id=None, event_type="tournament_reward_deleted", meta={"reward_id": str(reward_id), "admin_email": actor_email})


# ─── Eligibility ──────────────────────────────────────────────────────────────

def check_registration_eligibility(db: Session, tournament: CompetitiveTournament, user_id: UUID) -> None:
    """Raises HTTPException on failure. Authentication itself is already
    guaranteed by the router's Depends(get_current_user)."""
    already = db.exec(
        select(CompetitiveTournamentParticipant)
        .where(CompetitiveTournamentParticipant.tournament_id == tournament.id)
        .where(CompetitiveTournamentParticipant.user_id == user_id)
    ).first()
    if already is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tu es déjà inscrit à ce tournoi.")

    stats = db.get(CompetitiveStatistics, user_id)
    if stats is not None and stats.suspended_until is not None and stats.suspended_until > _now():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Ton accès à l'Arène compétitive est suspendu.")

    from app.services.competitive import moderation_service
    if moderation_service.has_active_sanction(db, user_id, "tournament_ban"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tu es banni des tournois.")

    # Academic level compatibility. Profile itself carries no levels.id FK —
    # only StudentProfile.level_main (a coarse enum: primary|bem|lycee|univ)
    # does. The `levels` catalogue's own `code` column is always seeded with
    # a "{level_main}_..." prefix (e.g. "lycee_1as", "bem_3am" — see
    # docs/migrations/002_complete_onboarding_data.sql), so a prefix match
    # between Level.code and StudentProfile.level_main is the reliable join
    # available given the current schema — deliberate interpretation, see
    # Phase 9 Part A report.
    if tournament.school_level_id is not None:
        level = db.get(Level, tournament.school_level_id)
        student = db.get(StudentProfile, user_id)
        if level is not None:
            if student is None or student.level_main is None or not level.code.startswith(f"{student.level_main}_"):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Ce tournoi est réservé à un autre niveau scolaire.")

    # Subject compatibility: tournament.subject_id being set is informational
    # only — no separate "subjects a student studies" ledger exists to check
    # against, so no additional check is applied here (see Phase 9 Part A
    # report for this interpretation).


# ─── Registration / waiting list ─────────────────────────────────────────────

def _registered_count(db: Session, tournament_id: UUID) -> int:
    return len(
        list(
            db.exec(
                select(CompetitiveTournamentParticipant)
                .where(CompetitiveTournamentParticipant.tournament_id == tournament_id)
                .where(CompetitiveTournamentParticipant.status == "registered")
            ).all()
        )
    )


def register_for_tournament(db: Session, tournament: CompetitiveTournament, user_id: UUID) -> CompetitiveTournamentParticipant:
    if tournament.status != "registration_open":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Les inscriptions ne sont pas ouvertes pour ce tournoi.")
    check_registration_eligibility(db, tournament, user_id)

    registered_count = _registered_count(db, tournament.id)
    if registered_count < tournament.max_participants:
        participant = CompetitiveTournamentParticipant(tournament_id=tournament.id, user_id=user_id, status="registered")
        event_type = "participant_registered"
    else:
        waitlist_count = len(
            list(
                db.exec(
                    select(CompetitiveTournamentParticipant)
                    .where(CompetitiveTournamentParticipant.tournament_id == tournament.id)
                    .where(CompetitiveTournamentParticipant.status == "waiting_list")
                ).all()
            )
        )
        participant = CompetitiveTournamentParticipant(
            tournament_id=tournament.id, user_id=user_id, status="waiting_list", waiting_list_position=waitlist_count + 1,
        )
        event_type = "participant_waitlisted"

    db.add(participant)
    db.commit()
    db.refresh(participant)
    log_event(db, tournament_id=tournament.id, actor_id=user_id, event_type=event_type, meta={"user_id": str(user_id)})

    try:
        from app.services.competitive import mission_service
        mission_service.record_event(db, user_id=user_id, metric_key="arena_tournament_joined", amount=1)
    except Exception:
        logger.exception("register_for_tournament: mission record_event failed user=%s", user_id)
    return participant


def unregister_from_tournament(db: Session, tournament: CompetitiveTournament, user_id: UUID) -> None:
    """Only allowed while registration is still open (or the tournament is
    merely published — an early opt-in the player may still cancel). Once
    registration_closed+, withdrawing must go through admin action instead
    (out of scope here — left for Part B's disqualify/replace action)."""
    if tournament.status not in ("published", "registration_open"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Il n'est plus possible de se désinscrire de ce tournoi.")

    participant = db.exec(
        select(CompetitiveTournamentParticipant)
        .where(CompetitiveTournamentParticipant.tournament_id == tournament.id)
        .where(CompetitiveTournamentParticipant.user_id == user_id)
    ).first()
    if participant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tu n'es pas inscrit à ce tournoi.")

    was_registered = participant.status == "registered"
    vacated_position = participant.waiting_list_position
    db.delete(participant)
    db.commit()
    log_event(db, tournament_id=tournament.id, actor_id=user_id, event_type="participant_unregistered", meta={"user_id": str(user_id), "was_registered": was_registered})

    if was_registered:
        # Promote the earliest waiting-list entry, if any.
        waiting = list(
            db.exec(
                select(CompetitiveTournamentParticipant)
                .where(CompetitiveTournamentParticipant.tournament_id == tournament.id)
                .where(CompetitiveTournamentParticipant.status == "waiting_list")
                .order_by(CompetitiveTournamentParticipant.waiting_list_position.asc())
            ).all()
        )
        if waiting:
            promoted = waiting[0]
            promoted.status = "registered"
            promoted.waiting_list_position = None
            db.add(promoted)
            for entry in waiting[1:]:
                if entry.waiting_list_position is not None:
                    entry.waiting_list_position -= 1
                    db.add(entry)
            db.commit()
            log_event(db, tournament_id=tournament.id, actor_id=None, event_type="participant_promoted_from_waitlist", meta={"user_id": str(promoted.user_id)})
    elif vacated_position is not None:
        # A waitlisted user left — renumber everyone behind them.
        behind = list(
            db.exec(
                select(CompetitiveTournamentParticipant)
                .where(CompetitiveTournamentParticipant.tournament_id == tournament.id)
                .where(CompetitiveTournamentParticipant.status == "waiting_list")
                .where(CompetitiveTournamentParticipant.waiting_list_position > vacated_position)
            ).all()
        )
        for entry in behind:
            entry.waiting_list_position -= 1
            db.add(entry)
        if behind:
            db.commit()


def list_participants(db: Session, tournament_id: UUID) -> List[Dict[str, Any]]:
    rows = list(
        db.exec(
            select(CompetitiveTournamentParticipant)
            .where(CompetitiveTournamentParticipant.tournament_id == tournament_id)
            .order_by(CompetitiveTournamentParticipant.seed.asc().nulls_last(), CompetitiveTournamentParticipant.registered_at.asc())
        ).all()
    )
    if not rows:
        return []
    profiles = {p.id: p for p in db.exec(select(Profile).where(Profile.id.in_({r.user_id for r in rows}))).all()}
    out = []
    for r in rows:
        profile = profiles.get(r.user_id)
        out.append({
            "id": r.id, "tournament_id": r.tournament_id, "user_id": r.user_id,
            "full_name": profile.full_name if profile else None,
            "avatar_url": profile.avatar_url if profile else None,
            "seed": r.seed, "status": r.status, "waiting_list_position": r.waiting_list_position,
            "registered_at": r.registered_at, "eliminated_at": r.eliminated_at,
            "final_round_reached": r.final_round_reached,
        })
    return out


# ─── Bracket generation & seeding (Single Elimination) ───────────────────────

def _seed_pairing_order(size: int) -> List[int]:
    """Standard "seed reflection" single-elimination pairing algorithm — the
    well-known recursive doubling sequence that guarantees seed 1 and seed 2
    can only ever meet in the final. For size=8: [1, 8, 4, 5, 2, 7, 3, 6],
    i.e. round-1 pairing order 1v8, 4v5, 2v7, 3v6."""
    seeds = [1]
    while len(seeds) < size:
        n = len(seeds) * 2
        seeds = [x for s in seeds for x in (s, n + 1 - s)]
    return seeds


_ROUND_NAMES = {
    1: "Finale",
    2: "Demi-finales",
    4: "Quarts de finale",
    8: "Huitièmes de finale",
    16: "Seizièmes de finale",
    32: "32es de finale",
    64: "64es de finale",
    128: "128es de finale",
}


def _round_name(matches_in_round: int) -> str:
    return _ROUND_NAMES.get(matches_in_round, f"Tour ({matches_in_round} matchs)")


def _status_for_slots(player_a_id: Optional[UUID], player_b_id: Optional[UUID]) -> tuple:
    """Returns (status, winner_id). Both present -> 'ready' (awaiting the
    real duel). Exactly one present -> 'bye' (auto-advance). Neither present
    -> 'bye' with winner_id=None (a "double bye" — an entire bracket branch
    with zero real players; only possible in a very sparse bracket)."""
    if player_a_id is not None and player_b_id is not None:
        return "ready", None
    if player_a_id is not None:
        return "bye", player_a_id
    if player_b_id is not None:
        return "bye", player_b_id
    return "bye", None


def generate_bracket(db: Session, tournament: CompetitiveTournament) -> CompetitiveTournament:
    """Transitions registration_closed -> bracket_generated. Pulls every
    status='registered' participant (waiting-list entries who never got
    promoted are simply not seeded), seeds them, builds the full bracket
    tree for every round up front, and synchronously cascades any BYE
    advancement (see _propagate_byes) — this is bracket-generation-time
    logic, not runtime progression, so it belongs here rather than in
    Part B's future progression engine."""
    if tournament.status != "registration_closed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Le bracket ne peut être généré qu'une fois les inscriptions clôturées.")

    registered = list(
        db.exec(
            select(CompetitiveTournamentParticipant)
            .where(CompetitiveTournamentParticipant.tournament_id == tournament.id)
            .where(CompetitiveTournamentParticipant.status == "registered")
        ).all()
    )
    if not registered:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Aucun participant inscrit — impossible de générer le bracket.")

    # ── 1-2. Seed ────────────────────────────────────────────────────────
    if tournament.seeding_method == "random":
        ordered = list(registered)
        random.shuffle(ordered)
    else:  # 'mmr' (default)
        def _rating(p: CompetitiveTournamentParticipant) -> int:
            stats = db.get(CompetitiveStatistics, p.user_id)
            return stats.rating if stats else 1000
        ordered = sorted(registered, key=lambda p: (-_rating(p), p.registered_at))

    for idx, participant in enumerate(ordered, start=1):
        participant.seed = idx
        db.add(participant)
    db.commit()

    seed_to_user: Dict[int, UUID] = {idx: p.user_id for idx, p in enumerate(ordered, start=1)}
    registered_count = len(ordered)

    # ── 3-4. Bracket size + standard seeding pairing order ───────────────
    bracket_size = tournament.max_participants
    seed_order = _seed_pairing_order(bracket_size)
    total_rounds = bracket_size.bit_length() - 1  # log2(bracket_size)

    # ── 5. Rounds ─────────────────────────────────────────────────────────
    rounds_by_number: Dict[int, CompetitiveTournamentRound] = {}
    for r in range(1, total_rounds + 1):
        matches_in_round = bracket_size // (2 ** r)
        round_row = CompetitiveTournamentRound(
            tournament_id=tournament.id, round_number=r, round_name=_round_name(matches_in_round), status="pending",
        )
        db.add(round_row)
        rounds_by_number[r] = round_row
    db.commit()
    for r in rounds_by_number:
        db.refresh(rounds_by_number[r])

    # ── 6. Matches — create from the FINAL backward so next_match_id can
    #      always point at an already-existing row. ───────────────────────
    matches_by_round: Dict[int, List[CompetitiveTournamentMatch]] = {}

    final_match = CompetitiveTournamentMatch(
        tournament_id=tournament.id, round_id=rounds_by_number[total_rounds].id, bracket_position=0,
        status="pending",
    )
    db.add(final_match)
    db.commit()
    db.refresh(final_match)
    matches_by_round[total_rounds] = [final_match]

    for r in range(total_rounds - 1, 0, -1):
        matches_in_round = bracket_size // (2 ** r)
        row_matches: List[CompetitiveTournamentMatch] = []
        for i in range(matches_in_round):
            next_match = matches_by_round[r + 1][i // 2]
            next_slot = "a" if i % 2 == 0 else "b"

            if r == 1:
                seed_a, seed_b = seed_order[2 * i], seed_order[2 * i + 1]
                player_a_id = seed_to_user.get(seed_a) if seed_a <= registered_count else None
                player_b_id = seed_to_user.get(seed_b) if seed_b <= registered_count else None
                match_status, winner_id = _status_for_slots(player_a_id, player_b_id)
            else:
                player_a_id = player_b_id = None
                match_status, winner_id = "pending", None

            match_row = CompetitiveTournamentMatch(
                tournament_id=tournament.id, round_id=rounds_by_number[r].id, bracket_position=i,
                player_a_id=player_a_id, player_b_id=player_b_id, status=match_status, winner_id=winner_id,
                next_match_id=next_match.id, next_match_slot=next_slot,
            )
            db.add(match_row)
            row_matches.append(match_row)
        db.commit()
        for m in row_matches:
            db.refresh(m)
        matches_by_round[r] = row_matches

    # ── 7-8. Cascade BYE advancement, then mark the tournament ready. ────
    _propagate_byes(db, tournament.id)

    tournament.status = "bracket_generated"
    tournament.updated_at = _now()
    db.add(tournament)
    db.commit()
    db.refresh(tournament)

    log_event(
        db, tournament_id=tournament.id, actor_id=None, event_type="bracket_generated",
        meta={"registered": registered_count, "bracket_size": bracket_size, "byes": bracket_size - registered_count},
    )
    publish_tournament_event(tournament.id, {"type": "bracket_updated"})
    for participant in ordered:
        emit(
            db, event_type="tournament_bracket_available", user_id=participant.user_id,
            context={"tournament_name": tournament.name},
            data={"tournament_id": str(tournament.id)},
            dedup_key=f"tournament_bracket_available:{tournament.id}:{participant.user_id}",
        )
    return tournament


def _propagate_byes(db: Session, tournament_id: UUID) -> None:
    """Walks every 'bye' match's winner into the next round's slot, then
    resolves the next round's own status once BOTH of its feeding matches
    are themselves already settled (status='bye') — this is what lets a
    bye cascade through several consecutive rounds in a very sparse bracket
    (e.g. 3 registrants in an 8-slot bracket). A feeder match still in
    'ready'/'pending' means real gameplay hasn't happened yet, so its
    dependent next-round match is deliberately left alone (Part B's
    progression engine fills it in once that real match finishes).

    Idempotent and side-effect-free if called again on an already-settled
    bracket — safe for Part B to call defensively too."""
    rounds = list(
        db.exec(
            select(CompetitiveTournamentRound)
            .where(CompetitiveTournamentRound.tournament_id == tournament_id)
            .order_by(CompetitiveTournamentRound.round_number.asc())
        ).all()
    )
    for rnd in rounds:
        matches = list(
            db.exec(
                select(CompetitiveTournamentMatch)
                .where(CompetitiveTournamentMatch.round_id == rnd.id)
                .order_by(CompetitiveTournamentMatch.bracket_position.asc())
            ).all()
        )
        if not matches:
            continue

        # Propagate this round's already-known bye winners forward.
        feeders_by_next: Dict[UUID, List[CompetitiveTournamentMatch]] = {}
        for m in matches:
            if m.next_match_id is None:
                continue
            feeders_by_next.setdefault(m.next_match_id, []).append(m)
            if m.status == "bye":
                nxt = db.get(CompetitiveTournamentMatch, m.next_match_id)
                if nxt is not None and nxt.status not in ("completed", "in_progress", "disqualified"):
                    if m.next_match_slot == "a" and nxt.player_a_id is None:
                        nxt.player_a_id = m.winner_id
                        db.add(nxt)
                    elif m.next_match_slot == "b" and nxt.player_b_id is None:
                        nxt.player_b_id = m.winner_id
                        db.add(nxt)
        db.commit()

        # Now finalize the NEXT round's matches whose both feeders are
        # already resolved (both 'bye').
        for next_match_id, feeders in feeders_by_next.items():
            nxt = db.get(CompetitiveTournamentMatch, next_match_id)
            if nxt is None or nxt.status in ("completed", "in_progress", "disqualified"):
                continue
            if not all(f.status == "bye" for f in feeders):
                continue  # at least one feeder still needs to actually be played
            match_status, winner_id = _status_for_slots(nxt.player_a_id, nxt.player_b_id)
            nxt.status = match_status
            nxt.winner_id = winner_id
            db.add(nxt)
        db.commit()


# ─── Bracket read (public) ────────────────────────────────────────────────────

def get_bracket_tree(db: Session, tournament: CompetitiveTournament) -> Dict[str, Any]:
    from app.services.competitive import spectator_presence_service

    rounds = list(
        db.exec(
            select(CompetitiveTournamentRound)
            .where(CompetitiveTournamentRound.tournament_id == tournament.id)
            .order_by(CompetitiveTournamentRound.round_number.asc())
        ).all()
    )
    all_matches = list(
        db.exec(
            select(CompetitiveTournamentMatch)
            .where(CompetitiveTournamentMatch.tournament_id == tournament.id)
            .order_by(CompetitiveTournamentMatch.bracket_position.asc())
        ).all()
    )

    user_ids = {m.player_a_id for m in all_matches if m.player_a_id} | {m.player_b_id for m in all_matches if m.player_b_id}
    profiles = {p.id: p for p in db.exec(select(Profile).where(Profile.id.in_(user_ids))).all()} if user_ids else {}
    seeds = {
        p.user_id: p.seed
        for p in db.exec(
            select(CompetitiveTournamentParticipant).where(CompetitiveTournamentParticipant.tournament_id == tournament.id)
        ).all()
    }

    duel_ids = [m.match_id for m in all_matches if m.match_id]
    duels = {d.id: d for d in db.exec(select(CompetitiveMatch).where(CompetitiveMatch.id.in_(duel_ids))).all()} if duel_ids else {}
    live_duel_ids = [d.id for d in duels.values() if d.status == "in_progress"]
    spectator_counts = spectator_presence_service.get_counts(live_duel_ids) if live_duel_ids else {}

    def _player(user_id: Optional[UUID]) -> Optional[Dict[str, Any]]:
        if user_id is None:
            return None
        profile = profiles.get(user_id)
        stats = db.get(CompetitiveStatistics, user_id)
        league = None
        if stats and stats.league_id:
            from app.models.competitive import CompetitiveLeague
            league = db.get(CompetitiveLeague, stats.league_id)
        return {
            "user_id": user_id, "full_name": profile.full_name if profile else None,
            "avatar_url": profile.avatar_url if profile else None, "seed": seeds.get(user_id),
            "league": league,
        }

    matches_by_round: Dict[UUID, List[Dict[str, Any]]] = {}
    for m in all_matches:
        duel = duels.get(m.match_id) if m.match_id else None
        score_a = score_b = None
        if duel is not None:
            participants = match_service.get_participants(db, duel.id)
            by_user = {p.user_id: p.score for p in participants}
            score_a = by_user.get(m.player_a_id)
            score_b = by_user.get(m.player_b_id)
        matches_by_round.setdefault(m.round_id, []).append({
            "id": m.id, "round_id": m.round_id, "bracket_position": m.bracket_position,
            "player_a": _player(m.player_a_id), "player_b": _player(m.player_b_id),
            "match_id": m.match_id, "winner_id": m.winner_id, "status": m.status,
            "next_match_id": m.next_match_id, "next_match_slot": m.next_match_slot,
            "scheduled_at": m.scheduled_at, "score_a": score_a, "score_b": score_b,
            "spectator_count": spectator_counts.get(str(m.match_id), 0) if m.match_id else 0,
        })

    rounds_out = []
    for rnd in rounds:
        rounds_out.append({
            "id": rnd.id, "round_number": rnd.round_number, "round_name": rnd.round_name,
            "status": rnd.status, "starts_at": rnd.starts_at, "completed_at": rnd.completed_at,
            "matches": sorted(matches_by_round.get(rnd.id, []), key=lambda x: x["bracket_position"]),
        })

    return {
        "tournament_id": tournament.id, "format": tournament.format,
        "max_participants": tournament.max_participants, "status": tournament.status, "rounds": rounds_out,
    }


# ─── Phase 9 — Tournament System (Single Elimination), Part B ───────────────
#
# The progression engine: starting the tournament/each round, creating the
# real CompetitiveMatch duel for a bracket slot, advancing a winner into the
# next round once a real match resolves, reward distribution, and the admin
# disqualify/replace/restart/advance actions. See gameplay_service.
# finish_match's Phase 9 hook for the entry point that calls
# resolve_tournament_match() after a normal/forfeited match completes.

def _total_rounds(tournament: CompetitiveTournament) -> int:
    return tournament.max_participants.bit_length() - 1


def _round_by_number(db: Session, tournament_id: UUID, round_number: int) -> Optional[CompetitiveTournamentRound]:
    return db.exec(
        select(CompetitiveTournamentRound)
        .where(CompetitiveTournamentRound.tournament_id == tournament_id)
        .where(CompetitiveTournamentRound.round_number == round_number)
    ).first()


def _matches_in_round(db: Session, round_id: UUID) -> List[CompetitiveTournamentMatch]:
    return list(
        db.exec(
            select(CompetitiveTournamentMatch)
            .where(CompetitiveTournamentMatch.round_id == round_id)
            .order_by(CompetitiveTournamentMatch.bracket_position.asc())
        ).all()
    )


# ─── Task 1 — Starting the tournament / a round ─────────────────────────────

def start_tournament(db: Session, tournament_id: UUID, *, actor_email: Optional[str] = None) -> CompetitiveTournament:
    """The missing link between 'bracket_generated' and real gameplay.
    Transitions the tournament to 'running' then kicks off round 1 (which
    itself recursively cascades past any fully-bye round — see
    _start_round)."""
    tournament = get_tournament_or_404(db, tournament_id)
    transition(tournament, "running")
    db.add(tournament)
    db.commit()
    db.refresh(tournament)
    log_event(db, tournament_id=tournament.id, actor_id=None, event_type="tournament_started", meta={"admin_email": actor_email})
    _start_round(db, tournament, 1)
    db.refresh(tournament)
    return tournament


def create_tournament_match_duel(db: Session, tournament: CompetitiveTournament, tm: CompetitiveTournamentMatch) -> CompetitiveMatch:
    """Mirrors match_service.create_matched_pair (Phase 6) — both players of
    a bracket slot are already known (not an invitation flow), so the duel
    is created directly in 'accepted' status with both participants at
    once, skipping 'waiting_for_opponent' entirely. match_type='tournament'
    (not 'duel') — the whole gameplay/replay/anti-abuse/presence/spectator
    engine is already match-type-agnostic (Phase 9 Part A's audit), so this
    is safe and lets every other part of the platform filter tournament
    matches distinctly wherever it wants to.

    Reuses tournament.rules (questions_per_match/difficulty) to parameterize
    the duel, falling back to tournament-level subject_id/school_level_id/
    difficulty for anything the rules blob doesn't override. Note: rules'
    'timer' key has no dedicated CompetitiveMatch column to hook into —
    per-question timing is entirely PlatformSettings-driven platform-wide,
    there is no per-match override column anywhere in this schema — an
    acknowledged gap, see Phase 9 Part B report, rather than a fabricated
    partial implementation.

    Sets tm.match_id and transitions tm.status (the BRACKET slot's own
    status, not the underlying CompetitiveMatch's lifecycle status) to
    'in_progress', or 'scheduled' first if tm.scheduled_at is set in the
    future — respecting per-match scheduling; immediate by default."""
    if tm.player_a_id is None or tm.player_b_id is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Les deux joueurs de ce match ne sont pas encore connus.")

    rules = tournament.rules or {}
    subject_ids = [tournament.subject_id] if tournament.subject_id else []
    difficulty = rules.get("difficulty") or tournament.difficulty
    question_count = rules.get("questions_per_match") or 10

    duel = CompetitiveMatch(
        match_type="tournament", creator_id=tm.player_a_id, created_by=tm.player_a_id,
        subject_ids=subject_ids, school_level_id=tournament.school_level_id,
        difficulty=difficulty, question_count=question_count, visibility="private", max_players=2,
        scheduled_at=tm.scheduled_at, max_spectators=tournament.max_spectators_default,
    )
    db.add(duel)
    db.commit()
    db.refresh(duel)

    db.add(CompetitiveMatchParticipant(match_id=duel.id, user_id=tm.player_a_id))
    db.add(CompetitiveMatchParticipant(match_id=duel.id, user_id=tm.player_b_id))
    match_service.transition(duel, "waiting_for_opponent")
    match_service.transition(duel, "accepted")
    is_future_scheduled = bool(tm.scheduled_at and tm.scheduled_at > _now())
    if is_future_scheduled:
        match_service.transition(duel, "scheduled")
    db.add(duel)
    db.commit()
    db.refresh(duel)

    tm.match_id = duel.id
    tm.status = "scheduled" if is_future_scheduled else "in_progress"
    tm.updated_at = _now()
    db.add(tm)
    db.commit()
    db.refresh(tm)

    log_event(
        db, tournament_id=tournament.id, actor_id=None, event_type="tournament_match_duel_created",
        meta={"tournament_match_id": str(tm.id), "match_id": str(duel.id)},
    )
    publish_tournament_event(tournament.id, {
        "type": "match_created", "tournament_match_id": str(tm.id), "match_id": str(duel.id),
    })

    for uid in (tm.player_a_id, tm.player_b_id):
        emit(
            db, event_type="tournament_match_starting_soon", user_id=uid,
            context={"tournament_name": tournament.name},
            data={"tournament_id": str(tournament.id), "tournament_match_id": str(tm.id), "match_id": str(duel.id)},
            dedup_key=f"tournament_match_starting_soon:{tm.id}:{uid}",
        )
    return duel


def _start_round(db: Session, tournament: CompetitiveTournament, round_number: int) -> None:
    """Starts a round: marks it in_progress, creates the real duel for every
    'ready' match in it. If, after that, the ENTIRE round turns out to
    already be bye/completed with nothing that was actually started (a
    fully-cascaded round — e.g. a very sparse bracket), immediately
    completes it and cascades to the next round instead of leaving it
    dangling — recursive, guarded against infinite loop via round_number
    never exceeding the tournament's total round count."""
    total_rounds = _total_rounds(tournament)
    if round_number > total_rounds:
        return  # guard — should never happen, defensive only

    round_row = _round_by_number(db, tournament.id, round_number)
    if round_row is None or round_row.status == "completed":
        return

    matches = _matches_in_round(db, round_row.id)
    if round_row.status != "in_progress":
        round_row.status = "in_progress"
        if round_row.starts_at is None:
            round_row.starts_at = _now()
        db.add(round_row)
        db.commit()
        db.refresh(round_row)
        log_event(db, tournament_id=tournament.id, actor_id=None, event_type="tournament_round_started", meta={"round_number": round_number})
        publish_tournament_event(tournament.id, {"type": "round_started", "round_number": round_number})

    created_any = False
    for m in matches:
        if m.status == "ready":
            create_tournament_match_duel(db, tournament, m)
            created_any = True

    refreshed = _matches_in_round(db, round_row.id)
    if refreshed and all(m.status in ("completed", "bye", "disqualified") for m in refreshed):
        # Fully pre-resolved round (every match was a bye, or already
        # disqualified/completed some other way) — nothing real was ever
        # played here. Close it out and cascade immediately.
        db.refresh(round_row)
        if round_row.status != "completed":
            round_row.status = "completed"
            round_row.completed_at = _now()
            db.add(round_row)
            db.commit()
            log_event(db, tournament_id=tournament.id, actor_id=None, event_type="tournament_round_completed", meta={"round_number": round_number, "auto_cascaded": True})
            publish_tournament_event(tournament.id, {"type": "round_completed", "round_number": round_number})

        next_round_number = round_number + 1
        if next_round_number <= total_rounds:
            _start_round(db, tournament, next_round_number)
        elif not created_any:
            # This WAS the final round and it resolved entirely via byes —
            # an extremely sparse bracket with zero real matches ever
            # played. Crown the champion directly from the final's
            # pre-resolved winner_id (may be None for a true double-bye,
            # in which case there is nobody to crown — logged, not faked).
            final_match = refreshed[0]
            if final_match.winner_id is not None and tournament.status == "running":
                loser_id = final_match.player_a_id if final_match.player_a_id != final_match.winner_id else final_match.player_b_id
                _complete_tournament(db, tournament, champion_id=final_match.winner_id, runner_up_id=loser_id)
            else:
                logger.warning("tournament %s final round resolved via byes with no determinable champion", tournament.id)


# ─── Task 2 — Progression engine (winner advancement) ───────────────────────

def resolve_draw_winner(
    db: Session, tournament: CompetitiveTournament, tm: CompetitiveTournamentMatch, results: Dict[UUID, str],
) -> Optional[UUID]:
    """Draws shouldn't normally happen in a tournament bracket —
    gameplay_service.finish_match's _decide_result already exhausts score ->
    accuracy -> avg response time -> fastest last correct answer before ever
    returning 'draw' — but a tournament match still needs exactly one winner
    to advance if it somehow does. Honors tournament.rules['tie_break_policy']
    when set to 'higher_seed' (or unset — that's the documented default
    fallback); any other/unknown policy value falls back to the same
    higher-seed rule rather than failing to produce a winner at all."""
    candidates = list(results.keys())
    if len(candidates) != 2:
        return candidates[0] if candidates else None
    a, b = candidates

    participants = list(
        db.exec(
            select(CompetitiveTournamentParticipant)
            .where(CompetitiveTournamentParticipant.tournament_id == tournament.id)
            .where(CompetitiveTournamentParticipant.user_id.in_([a, b]))
        ).all()
    )
    seeds = {p.user_id: p.seed for p in participants}
    seed_a, seed_b = seeds.get(a), seeds.get(b)
    if seed_a is not None and seed_b is not None and seed_a != seed_b:
        return a if seed_a < seed_b else b

    # Ultimate deterministic fallback: the bracket's own player_a slot.
    return tm.player_a_id if tm.player_a_id in (a, b) else a


def resolve_tournament_match(
    db: Session, tm: CompetitiveTournamentMatch, winner_id: UUID, *, actor_email: Optional[str] = None,
) -> CompetitiveTournamentMatch:
    """THE single shared resolution function: a tournament match has a
    winner, now propagate. Called from gameplay_service.finish_match's Phase
    9 hook (normal/forfeit completion), the admin disqualify/advance
    actions, and nowhere else — so there is exactly one place that
    implements "advance a winner". Idempotent-safe: if tm.status is already
    'completed' this is a no-op (guards against double-processing from any
    of those multiple entry points)."""
    if tm.status == "completed":
        logger.warning("resolve_tournament_match: tournament_match=%s already completed — no-op", tm.id)
        return tm

    tournament = get_tournament_or_404(db, tm.tournament_id)
    round_row = db.get(CompetitiveTournamentRound, tm.round_id)

    loser_id: Optional[UUID] = None
    if tm.player_a_id is not None and tm.player_a_id != winner_id:
        loser_id = tm.player_a_id
    elif tm.player_b_id is not None and tm.player_b_id != winner_id:
        loser_id = tm.player_b_id

    tm.winner_id = winner_id
    tm.status = "completed"
    tm.updated_at = _now()
    db.add(tm)
    db.commit()
    db.refresh(tm)

    if loser_id is not None:
        loser_participant = db.exec(
            select(CompetitiveTournamentParticipant)
            .where(CompetitiveTournamentParticipant.tournament_id == tournament.id)
            .where(CompetitiveTournamentParticipant.user_id == loser_id)
        ).first()
        if loser_participant is not None and loser_participant.status not in ("disqualified", "withdrawn"):
            loser_participant.status = "eliminated"
            loser_participant.eliminated_at = _now()
            loser_participant.final_round_reached = round_row.round_number if round_row else loser_participant.final_round_reached
            db.add(loser_participant)
            db.commit()

    log_event(
        db, tournament_id=tournament.id, actor_id=None, event_type="tournament_match_resolved",
        meta={
            "tournament_match_id": str(tm.id), "winner_id": str(winner_id),
            "loser_id": str(loser_id) if loser_id else None, "admin_email": actor_email,
        },
    )
    publish_tournament_event(tournament.id, {
        "type": "participant_advanced", "tournament_match_id": str(tm.id), "winner_id": str(winner_id),
    })

    if tm.next_match_id is None:
        # This was the Final — the tournament is over.
        if round_row is not None and round_row.status != "completed":
            round_row.status = "completed"
            round_row.completed_at = _now()
            db.add(round_row)
            db.commit()
        _complete_tournament(db, tournament, champion_id=winner_id, runner_up_id=loser_id)
        return tm

    next_match = db.get(CompetitiveTournamentMatch, tm.next_match_id)
    if next_match is not None and next_match.status not in ("completed", "disqualified"):
        if next_match.status == "bye":
            # Shouldn't normally happen this late — byes are resolved at
            # bracket-generation time — handled defensively, no-op.
            logger.warning("resolve_tournament_match: next_match=%s already 'bye' when a feeder just resolved — no-op", next_match.id)
        else:
            slot = tm.next_match_slot
            if slot == "a" and next_match.player_a_id is None:
                next_match.player_a_id = winner_id
            elif slot == "b" and next_match.player_b_id is None:
                next_match.player_b_id = winner_id
            db.add(next_match)
            db.commit()
            db.refresh(next_match)

            # Only ever finalize to 'ready' once BOTH slots are genuinely
            # filled — calling _status_for_slots with only one slot filled
            # would misreport this as a 'bye' (that helper's semantics only
            # make sense at bracket-generation time, when every input is
            # already fully known; here the other slot may simply still be
            # awaiting its own real match). Left 'pending' otherwise.
            if next_match.player_a_id is not None and next_match.player_b_id is not None:
                new_status, _ = _status_for_slots(next_match.player_a_id, next_match.player_b_id)
                if new_status == "ready":
                    next_match.status = "ready"
                    db.add(next_match)
                    db.commit()
                    db.refresh(next_match)
                    publish_tournament_event(tournament.id, {"type": "bracket_updated"})
                    next_round = db.get(CompetitiveTournamentRound, next_match.round_id)
                    if next_round is not None and next_round.status == "in_progress":
                        # Both feeders resolved at different times — this
                        # round already started, so this later-round match
                        # can start right now instead of waiting for the
                        # whole round to be (re)activated.
                        create_tournament_match_duel(db, tournament, next_match)
                else:
                    logger.warning("resolve_tournament_match: unexpected status=%s for fully-filled next_match=%s", new_status, next_match.id)

    _check_round_completion(db, tournament, round_row)
    return tm


def _check_round_completion(db: Session, tournament: CompetitiveTournament, round_row: Optional[CompetitiveTournamentRound]) -> None:
    if round_row is None or round_row.status == "completed":
        return
    matches = _matches_in_round(db, round_row.id)
    if not matches or not all(m.status in ("completed", "bye", "disqualified") for m in matches):
        return

    round_row.status = "completed"
    round_row.completed_at = _now()
    db.add(round_row)
    db.commit()
    log_event(db, tournament_id=tournament.id, actor_id=None, event_type="tournament_round_completed", meta={"round_number": round_row.round_number})
    publish_tournament_event(tournament.id, {"type": "round_completed", "round_number": round_row.round_number})

    total_rounds = _total_rounds(tournament)
    next_round_number = round_row.round_number + 1
    if next_round_number <= total_rounds:
        next_round = _round_by_number(db, tournament.id, next_round_number)
        if next_round is not None and next_round.status == "pending":
            _start_round(db, tournament, next_round_number)


def _complete_tournament(db: Session, tournament: CompetitiveTournament, *, champion_id: Optional[UUID], runner_up_id: Optional[UUID]) -> None:
    if champion_id is not None:
        champ = db.exec(
            select(CompetitiveTournamentParticipant)
            .where(CompetitiveTournamentParticipant.tournament_id == tournament.id)
            .where(CompetitiveTournamentParticipant.user_id == champion_id)
        ).first()
        if champ is not None:
            champ.status = "champion"
            db.add(champ)
    if runner_up_id is not None:
        runner_up = db.exec(
            select(CompetitiveTournamentParticipant)
            .where(CompetitiveTournamentParticipant.tournament_id == tournament.id)
            .where(CompetitiveTournamentParticipant.user_id == runner_up_id)
        ).first()
        if runner_up is not None:
            runner_up.status = "runner_up"
            db.add(runner_up)
    db.commit()

    transition(tournament, "completed")
    db.add(tournament)
    db.commit()
    db.refresh(tournament)

    log_event(
        db, tournament_id=tournament.id, actor_id=None, event_type="tournament_completed",
        meta={"champion_id": str(champion_id) if champion_id else None, "runner_up_id": str(runner_up_id) if runner_up_id else None},
    )
    publish_tournament_event(tournament.id, {
        "type": "tournament_completed", "champion_id": str(champion_id) if champion_id else None,
    })

    try:
        distribute_tournament_rewards(db, tournament)
    except Exception:
        logger.exception("distribute_tournament_rewards failed tournament=%s", tournament.id)

    try:
        if champion_id is not None:
            emit(
                db, event_type="tournament_champion_announced", user_id=champion_id,
                context={"tournament_name": tournament.name},
                data={"tournament_id": str(tournament.id), "champion_id": str(champion_id)},
                dedup_key=f"tournament_champion_announced:{tournament.id}:{champion_id}",
            )
        if runner_up_id is not None:
            emit(
                db, event_type="tournament_champion_announced", user_id=runner_up_id,
                context={"tournament_name": tournament.name},
                data={"tournament_id": str(tournament.id), "champion_id": str(champion_id) if champion_id else None},
                dedup_key=f"tournament_champion_announced:{tournament.id}:{runner_up_id}",
            )
        # Cheap given the participant list is already local (season_service.
        # _notify_season_started precedent) — notify every other participant
        # too, not just the podium.
        others = list(
            db.exec(
                select(CompetitiveTournamentParticipant)
                .where(CompetitiveTournamentParticipant.tournament_id == tournament.id)
            ).all()
        )
        for participant in others:
            if participant.user_id in (champion_id, runner_up_id):
                continue
            emit(
                db, event_type="tournament_champion_announced", user_id=participant.user_id,
                context={"tournament_name": tournament.name},
                data={"tournament_id": str(tournament.id), "champion_id": str(champion_id) if champion_id else None},
                dedup_key=f"tournament_champion_announced:{tournament.id}:{participant.user_id}",
            )
    except Exception:
        logger.exception("tournament_champion_announced notifications failed tournament=%s", tournament.id)


# ─── Task 3 — Reward distribution ───────────────────────────────────────────

def _apply_arena_rating_bonus(db: Session, *, user_id: UUID, amount: int) -> None:
    """arena_rating is a flat bonus applied ON TOP of the rating each real
    match already independently adjusted via ranking_service's per-match Elo
    delta — never a re-run of that calculation. Clamped at the same
    competitive_mmr_floor every other rating write respects, then league_id/
    rank_tier/highest_rating are re-derived exactly like ranking_service does
    after any other rating change."""
    from app.models.admin import PlatformSettings
    from app.services.competitive import ranking_service, statistics_service

    stats = db.get(CompetitiveStatistics, user_id) or statistics_service.get_or_create_statistics(db, user_id)
    settings_row = db.get(PlatformSettings, True) or PlatformSettings()
    new_rating = max(settings_row.competitive_mmr_floor, stats.rating + amount)
    stats.rating = new_rating
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


def _record_tournament_grant(db: Session, *, tournament: CompetitiveTournament, user_id: UUID, reward: CompetitiveTournamentReward, status_: str) -> None:
    """For reward_type values reward_service.grant_reward doesn't natively
    apply (arena_rating/arena_xp/season_points — Phase 7's _LIVE_REWARD_TYPES
    is only {ep, badge}) — writes into the SAME competitive_reward_grants
    audit trail directly (source='tournament', tournament_id set — migration
    085), so a student's "my rewards" view stays unified across season-end
    and tournament grants rather than needing two separate tables."""
    grant = CompetitiveRewardGrant(
        user_id=user_id, season_id=None, tournament_id=tournament.id, source="tournament",
        reward_type=reward.reward_type, reward_ref=reward.reward_ref, reward_amount=reward.reward_amount,
        status=status_,
    )
    db.add(grant)
    db.commit()
    db.refresh(grant)
    try:
        emit(
            db, event_type="competitive_reward_received", user_id=user_id,
            data={
                "reward_type": reward.reward_type, "reward_ref": reward.reward_ref,
                "reward_amount": reward.reward_amount, "source": "tournament", "tournament_id": str(tournament.id),
            },
            dedup_key=f"competitive_reward_received:{grant.id}",
        )
    except Exception:
        logger.exception("tournament reward notify failed user=%s tournament=%s", user_id, tournament.id)


def _grant_tournament_reward(db: Session, *, tournament: CompetitiveTournament, user_id: UUID, reward: CompetitiveTournamentReward) -> None:
    from app.services.competitive import reward_service, spectator_service

    rtype = reward.reward_type
    try:
        if rtype in ("ep", "badge", "sticker", "title", "frame"):
            # Reuses Phase 7's exact grant_reward primitive (EP live via
            # kp_service, badge live via UserBadge, everything else
            # 'recorded_only') — it already writes competitive_reward_grants
            # itself (source='tournament', tournament_id=... — migration 085).
            reward_service.grant_reward(
                db, user_id=user_id, season_id=None, source="tournament", reward_type=rtype,
                reward_ref=reward.reward_ref, reward_amount=reward.reward_amount,
                tournament_id=tournament.id,
            )
        elif rtype == "arena_rating":
            _apply_arena_rating_bonus(db, user_id=user_id, amount=reward.reward_amount or 0)
            _record_tournament_grant(db, tournament=tournament, user_id=user_id, reward=reward, status_="granted")
        elif rtype == "arena_xp":
            # Same single "Arena XP" counter Phase 8 already uses
            # (competitive_spectator_stats.arena_xp) — not a tournament-
            # specific counter.
            stats = spectator_service.get_or_create_spectator_stats(db, user_id)
            stats.arena_xp += reward.reward_amount or 0
            stats.updated_at = _now()
            db.add(stats)
            db.commit()
            _record_tournament_grant(db, tournament=tournament, user_id=user_id, reward=reward, status_="granted")
        elif rtype == "season_points":
            # No distinct "season points" ledger exists separate from
            # competitive_season_stats.mmr (which every real match already
            # updates independently via season_service.record_match_for_
            # season) — there is no separate ranking-points column to bump.
            # Recorded-only for audit purposes; see Phase 9 Part B report.
            _record_tournament_grant(db, tournament=tournament, user_id=user_id, reward=reward, status_="recorded_only")
        else:
            _record_tournament_grant(db, tournament=tournament, user_id=user_id, reward=reward, status_="recorded_only")
    except Exception:
        logger.exception("distribute_tournament_rewards: granting reward_type=%s failed for user=%s tournament=%s", rtype, user_id, tournament.id)

    log_event(
        db, tournament_id=tournament.id, actor_id=None, event_type="tournament_reward_granted",
        meta={
            "user_id": str(user_id), "placement": reward.placement, "reward_type": rtype,
            "reward_ref": reward.reward_ref, "reward_amount": reward.reward_amount,
        },
    )


def distribute_tournament_rewards(db: Session, tournament: CompetitiveTournament) -> int:
    """Called once, from _complete_tournament — matches every configured
    competitive_tournament_rewards row against the participant(s) whose final
    placement it targets, and grants it."""
    rewards = list_rewards(db, tournament.id)
    if not rewards:
        return 0

    total_rounds = _total_rounds(tournament)
    participants = list(
        db.exec(select(CompetitiveTournamentParticipant).where(CompetitiveTournamentParticipant.tournament_id == tournament.id)).all()
    )

    granted = 0
    for reward in rewards:
        if reward.placement == "champion":
            targets = [p for p in participants if p.status == "champion"]
        elif reward.placement == "runner_up":
            targets = [p for p in participants if p.status == "runner_up"]
        elif reward.placement == "semifinalist":
            targets = [p for p in participants if p.final_round_reached == total_rounds - 1]
        elif reward.placement == "quarterfinalist":
            targets = [p for p in participants if p.final_round_reached == total_rounds - 2]
        elif reward.placement == "participation":
            targets = [p for p in participants if p.status != "waiting_list"]
        else:
            targets = []

        for participant in targets:
            _grant_tournament_reward(db, tournament=tournament, user_id=participant.user_id, reward=reward)
            granted += 1
    return granted


# ─── Task 4 — Admin actions: disqualify / replace / restart / advance ───────

def admin_disqualify_participant(
    db: Session, tournament_id: UUID, tournament_match_id: UUID, *, user_id: UUID, reason: Optional[str] = None, actor_email: Optional[str] = None,
) -> CompetitiveTournamentMatch:
    tournament = get_tournament_or_404(db, tournament_id)
    tm = db.get(CompetitiveTournamentMatch, tournament_match_id)
    if tm is None or tm.tournament_id != tournament_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match de tournoi introuvable.")
    if tm.status in ("completed", "disqualified"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce match est déjà terminé — utilise /advance pour une correction manuelle.")
    if user_id not in (tm.player_a_id, tm.player_b_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Ce joueur ne participe pas à ce match.")

    participant = db.exec(
        select(CompetitiveTournamentParticipant)
        .where(CompetitiveTournamentParticipant.tournament_id == tournament_id)
        .where(CompetitiveTournamentParticipant.user_id == user_id)
    ).first()
    if participant is not None:
        participant.status = "disqualified"
        participant.eliminated_at = _now()
        round_row = db.get(CompetitiveTournamentRound, tm.round_id)
        participant.final_round_reached = round_row.round_number if round_row else participant.final_round_reached
        db.add(participant)
        db.commit()

    if tm.match_id is not None:
        duel = db.get(CompetitiveMatch, tm.match_id)
        if duel is not None and duel.status not in ("completed", "cancelled", "expired", "abandoned"):
            match_service.transition(duel, "cancelled")
            duel.cancelled_at = _now()
            duel.cancelled_reason = f"Disqualification en tournoi : {reason}" if reason else "Disqualification en tournoi."
            db.add(duel)
            db.commit()

    log_event(
        db, tournament_id=tournament_id, actor_id=None, event_type="tournament_participant_disqualified",
        meta={"user_id": str(user_id), "reason": reason, "admin_email": actor_email, "tournament_match_id": str(tm.id)},
    )

    other_id = tm.player_b_id if user_id == tm.player_a_id else tm.player_a_id
    if other_id is not None:
        # The bye-adjacent edge case ("disqualified player was player_b with
        # no player_a", or vice versa) collapses to this exact same branch —
        # the other slot's occupant just advances normally.
        result_tm = resolve_tournament_match(db, tm, other_id, actor_email=actor_email)
        publish_tournament_event(tournament_id, {"type": "bracket_updated"})
        return result_tm

    # Both slots empty of a valid winner (the OTHER slot was never filled at
    # all — the disqualified player's opponent hasn't been determined by an
    # earlier round yet). No winner to advance; leave for manual admin
    # resolution via /advance once the bracket makes that possible.
    tm.status = "disqualified"
    tm.updated_at = _now()
    db.add(tm)
    db.commit()
    db.refresh(tm)
    publish_tournament_event(tournament_id, {"type": "bracket_updated"})
    return tm


def admin_replace_participant(
    db: Session, tournament_id: UUID, tournament_match_id: UUID, *, old_user_id: UUID, new_user_id: UUID, actor_email: Optional[str] = None,
) -> CompetitiveTournamentMatch:
    """Only allowed while no real match exists yet for this slot
    (status in pending/ready). Interpreted per the spec's "Replace
    participant" intent as swapping in another ALREADY-REGISTERED
    participant of this same tournament (e.g. a waitlisted or already-
    eliminated one) — not opening a brand-new outside registration mid-
    bracket, which check_registration_eligibility isn't designed for at
    this stage anyway."""
    get_tournament_or_404(db, tournament_id)
    tm = db.get(CompetitiveTournamentMatch, tournament_match_id)
    if tm is None or tm.tournament_id != tournament_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match de tournoi introuvable.")
    if tm.status not in ("pending", "ready"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Remplacement possible uniquement avant le début du match réel.")
    if old_user_id not in (tm.player_a_id, tm.player_b_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Le joueur à remplacer ne participe pas à ce match.")

    new_participant = db.exec(
        select(CompetitiveTournamentParticipant)
        .where(CompetitiveTournamentParticipant.tournament_id == tournament_id)
        .where(CompetitiveTournamentParticipant.user_id == new_user_id)
    ).first()
    if new_participant is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Le joueur de remplacement doit déjà être inscrit à ce tournoi.")
    if new_participant.status not in ("registered", "waiting_list", "eliminated"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce participant ne peut pas être inséré dans le bracket (statut incompatible).")

    if tm.player_a_id == old_user_id:
        tm.player_a_id = new_user_id
    else:
        tm.player_b_id = new_user_id

    # Both slots filled -> 'ready'; otherwise stays 'pending' (never 'bye' —
    # that status is purely a bracket-generation-time concept, see
    # resolve_tournament_match's comment on the same subtlety).
    tm.status = "ready" if (tm.player_a_id is not None and tm.player_b_id is not None) else "pending"
    tm.winner_id = None
    tm.updated_at = _now()
    db.add(tm)
    db.commit()
    db.refresh(tm)

    log_event(
        db, tournament_id=tournament_id, actor_id=None, event_type="tournament_participant_replaced",
        meta={"tournament_match_id": str(tm.id), "old_user_id": str(old_user_id), "new_user_id": str(new_user_id), "admin_email": actor_email},
    )
    publish_tournament_event(tournament_id, {"type": "bracket_updated"})
    return tm


def admin_advance_match(db: Session, tournament_id: UUID, tournament_match_id: UUID, *, winner_id: UUID, actor_email: Optional[str] = None) -> CompetitiveTournamentMatch:
    """Admin override for disputes/edge cases where the real match can't
    cleanly determine a winner — calls the exact same shared
    resolve_tournament_match() every other resolution path uses."""
    get_tournament_or_404(db, tournament_id)
    tm = db.get(CompetitiveTournamentMatch, tournament_match_id)
    if tm is None or tm.tournament_id != tournament_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match de tournoi introuvable.")
    if tm.status == "completed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce match est déjà terminé.")
    if winner_id not in (tm.player_a_id, tm.player_b_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="winner_id doit être un des deux joueurs de ce match.")

    result_tm = resolve_tournament_match(db, tm, winner_id, actor_email=actor_email)
    publish_tournament_event(tournament_id, {"type": "bracket_updated"})
    return result_tm


def admin_restart_match(db: Session, tournament_id: UUID, tournament_match_id: UUID, *, actor_email: Optional[str] = None) -> CompetitiveTournamentMatch:
    tournament = get_tournament_or_404(db, tournament_id)
    tm = db.get(CompetitiveTournamentMatch, tournament_match_id)
    if tm is None or tm.tournament_id != tournament_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match de tournoi introuvable.")
    if tm.status == "bye":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Un bye ne peut pas être redémarré.")

    if tm.match_id is not None:
        duel = db.get(CompetitiveMatch, tm.match_id)
        if duel is not None and duel.status not in ("completed", "cancelled", "expired", "abandoned"):
            match_service.transition(duel, "cancelled")
            duel.cancelled_at = _now()
            duel.cancelled_reason = "Match de tournoi redémarré par un administrateur."
            db.add(duel)
            db.commit()

    tm.match_id = None
    tm.winner_id = None
    tm.status = "ready" if (tm.player_a_id is not None and tm.player_b_id is not None) else "pending"
    tm.updated_at = _now()
    db.add(tm)
    db.commit()
    db.refresh(tm)

    log_event(
        db, tournament_id=tournament_id, actor_id=None, event_type="tournament_match_restarted",
        meta={"tournament_match_id": str(tm.id), "admin_email": actor_email},
    )
    publish_tournament_event(tournament_id, {"type": "bracket_updated"})

    if tm.status == "ready":
        round_row = db.get(CompetitiveTournamentRound, tm.round_id)
        if round_row is not None and round_row.status == "in_progress":
            create_tournament_match_duel(db, tournament, tm)
            db.refresh(tm)
    return tm


# ─── Task 7 — Player-facing read endpoints Part A didn't build ─────────────

def get_tournament_history(db: Session, tournament: CompetitiveTournament) -> Dict[str, Any]:
    """Full final standings for a completed tournament + a pointer to each
    participant's played matches (match_id) so the caller can reuse Phase
    4's existing replay endpoints (GET /competitive/matches/{id}/replay) —
    replay content itself is never duplicated here."""
    participants = list_participants(db, tournament.id)
    all_matches = list(
        db.exec(select(CompetitiveTournamentMatch).where(CompetitiveTournamentMatch.tournament_id == tournament.id)).all()
    )
    matches_by_user: Dict[UUID, List[Dict[str, Any]]] = {}
    for m in all_matches:
        if m.match_id is None:
            continue
        for uid in (m.player_a_id, m.player_b_id):
            if uid is None:
                continue
            matches_by_user.setdefault(uid, []).append({
                "tournament_match_id": m.id, "match_id": m.match_id, "won": m.winner_id == uid,
                "replay_endpoint": f"/api/competitive/matches/{m.match_id}/replay",
            })

    standings = [
        {**p, "matches": matches_by_user.get(p["user_id"], [])}
        for p in participants
    ]
    standings.sort(key=lambda x: (x["seed"] is None, x["seed"] or 0))
    return {"tournament_id": tournament.id, "status": tournament.status, "standings": standings}


def get_user_tournament_history(db: Session, user_id: UUID) -> Dict[str, Any]:
    """Current user's own cross-tournament history: every tournament they
    participated in, their result, and every tournament-sourced reward
    grant (competitive_reward_grants, source='tournament' — the same shared
    ledger Phase 7 uses for season-end/promotion grants, so this is a
    unified 'my rewards' view, not a parallel one)."""
    rows = list(
        db.exec(
            select(CompetitiveTournamentParticipant)
            .where(CompetitiveTournamentParticipant.user_id == user_id)
            .order_by(CompetitiveTournamentParticipant.registered_at.desc())
        ).all()
    )
    tournaments_out: List[Dict[str, Any]] = []
    if rows:
        tournament_ids = {r.tournament_id for r in rows}
        tournaments = {t.id: t for t in db.exec(select(CompetitiveTournament).where(CompetitiveTournament.id.in_(tournament_ids))).all()}
        for r in rows:
            t = tournaments.get(r.tournament_id)
            if t is None:
                continue
            tournaments_out.append({
                "tournament_id": t.id, "tournament_name": t.name, "status": t.status,
                "seed": r.seed, "result": r.status, "final_round_reached": r.final_round_reached,
                "registered_at": r.registered_at, "eliminated_at": r.eliminated_at,
            })

    grants = list(
        db.exec(
            select(CompetitiveRewardGrant)
            .where(CompetitiveRewardGrant.user_id == user_id)
            .where(CompetitiveRewardGrant.source == "tournament")
            .order_by(CompetitiveRewardGrant.granted_at.desc())
        ).all()
    )
    rewards_out = [
        {
            "tournament_id": g.tournament_id, "reward_type": g.reward_type, "reward_ref": g.reward_ref,
            "reward_amount": g.reward_amount, "status": g.status, "granted_at": g.granted_at,
        }
        for g in grants
    ]
    return {"tournaments": tournaments_out, "rewards": rewards_out}


def get_tournament_statistics(db: Session, tournament: CompetitiveTournament) -> Dict[str, Any]:
    """Everything here is computed live via joins scoped to this
    tournament's own match_ids against the existing Phase 3/8 tables — no
    new table needed."""
    from app.models.competitive import (
        CompetitiveMatchQuestion,
        CompetitiveMatchReaction,
        CompetitivePrediction,
        CompetitiveQuestionAttempt,
        CompetitiveSpectator,
    )

    participants_count = len(
        list(db.exec(select(CompetitiveTournamentParticipant.id).where(CompetitiveTournamentParticipant.tournament_id == tournament.id)).all())
    )
    match_ids = list(
        db.exec(
            select(CompetitiveTournamentMatch.match_id)
            .where(CompetitiveTournamentMatch.tournament_id == tournament.id)
            .where(CompetitiveTournamentMatch.match_id.is_not(None))
        ).all()
    )
    if not match_ids:
        return {
            "tournament_id": tournament.id, "participants_count": participants_count, "matches_count": 0,
            "questions_answered": 0, "accuracy_pct": 0.0, "avg_match_duration_sec": None,
            "total_spectators": 0, "reactions_count": 0, "predictions_count": 0,
        }

    duels = list(db.exec(select(CompetitiveMatch).where(CompetitiveMatch.id.in_(match_ids))).all())
    durations = [(d.completed_at - d.started_at).total_seconds() for d in duels if d.completed_at and d.started_at]

    attempts = list(
        db.exec(
            select(CompetitiveQuestionAttempt)
            .join(CompetitiveMatchQuestion, CompetitiveMatchQuestion.id == CompetitiveQuestionAttempt.match_question_id)
            .where(CompetitiveMatchQuestion.match_id.in_(match_ids))
        ).all()
    )
    correct = sum(1 for a in attempts if a.is_correct)

    spectators = len(list(db.exec(select(CompetitiveSpectator.id).where(CompetitiveSpectator.match_id.in_(match_ids))).all()))
    reactions = len(list(db.exec(select(CompetitiveMatchReaction.id).where(CompetitiveMatchReaction.match_id.in_(match_ids))).all()))
    predictions = len(list(db.exec(select(CompetitivePrediction.id).where(CompetitivePrediction.match_id.in_(match_ids))).all()))

    return {
        "tournament_id": tournament.id, "participants_count": participants_count, "matches_count": len(duels),
        "questions_answered": len(attempts),
        "accuracy_pct": round((correct / len(attempts)) * 100, 2) if attempts else 0.0,
        "avg_match_duration_sec": round(sum(durations) / len(durations)) if durations else None,
        "total_spectators": spectators, "reactions_count": reactions, "predictions_count": predictions,
    }


def get_tournaments_analytics(db: Session) -> Dict[str, Any]:
    """Platform-wide, NOT scoped to one tournament — reasonable best-effort
    aggregation across every tournament ever created."""
    from app.models.competitive import CompetitiveSpectator

    tournaments = list(db.exec(select(CompetitiveTournament)).all())
    all_participants = list(db.exec(select(CompetitiveTournamentParticipant)).all())
    completed = [t for t in tournaments if t.status == "completed"]

    registered_count = sum(1 for p in all_participants if p.status != "waiting_list")
    played_count = sum(
        1 for p in all_participants
        if p.status != "waiting_list" and (p.final_round_reached is not None or p.status in ("champion", "runner_up"))
    )
    participation_rate = round((played_count / registered_count) * 100, 2) if registered_count else 0.0
    completion_rate = round((len(completed) / len(tournaments)) * 100, 2) if tournaments else 0.0

    subject_counts: Dict[str, int] = {}
    level_counts: Dict[str, int] = {}
    for t in tournaments:
        if t.subject_id:
            subject_counts[str(t.subject_id)] = subject_counts.get(str(t.subject_id), 0) + 1
        if t.school_level_id:
            level_counts[str(t.school_level_id)] = level_counts.get(str(t.school_level_id), 0) + 1
    top_subjects = sorted(subject_counts.items(), key=lambda kv: -kv[1])[:5]
    top_levels = sorted(level_counts.items(), key=lambda kv: -kv[1])[:5]

    durations = []
    for t in completed:
        rounds = list(
            db.exec(
                select(CompetitiveTournamentRound)
                .where(CompetitiveTournamentRound.tournament_id == t.id)
                .order_by(CompetitiveTournamentRound.round_number.asc())
            ).all()
        )
        if rounds and rounds[0].starts_at and rounds[-1].completed_at:
            durations.append((rounds[-1].completed_at - rounds[0].starts_at).total_seconds())

    rewards_distributed = len(list(db.exec(select(CompetitiveRewardGrant.id).where(CompetitiveRewardGrant.source == "tournament")).all()))

    tm_match_ids = list(
        db.exec(select(CompetitiveTournamentMatch.match_id).where(CompetitiveTournamentMatch.match_id.is_not(None))).all()
    )
    peak_spectators = 0
    if tm_match_ids:
        rows = db.exec(
            select(CompetitiveSpectator.match_id, func.count())
            .where(CompetitiveSpectator.match_id.in_(tm_match_ids))
            .group_by(CompetitiveSpectator.match_id)
        ).all()
        peak_spectators = max((c for _, c in rows), default=0)

    return {
        "total_tournaments": len(tournaments), "completed_tournaments": len(completed),
        "total_registrations": registered_count, "participation_rate_pct": participation_rate,
        "completion_rate_pct": completion_rate,
        "avg_duration_sec": round(sum(durations) / len(durations)) if durations else None,
        "top_subject_ids": [{"subject_id": s, "tournament_count": c} for s, c in top_subjects],
        "top_school_level_ids": [{"school_level_id": s, "tournament_count": c} for s, c in top_levels],
        "peak_spectators_single_match": peak_spectators,
        "rewards_distributed": rewards_distributed,
    }
