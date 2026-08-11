from __future__ import annotations

"""
Matchmaking Service — Competitive Arena Phase 6.

The actual pairing engine: search-radius expansion, match-quality scoring,
and the sweep that turns two compatible 'searching' entries into a
'matched' proposal. Deliberately independent from the Match Engine
(app/services/competitive/match_service.py, gameplay_service.py) — this
module never touches gameplay, only queue rows, mirroring the spec's
"dedicated Matchmaking Engine, independent from Match Engine" requirement.

Hard filters (never relaxed, never cross-subject): mode, subject_id,
school_level_id, difficulty must be IDENTICAL between two entries.
Soft/expanding filters: rating (current_radius, grows over time) and
language (exact preference first, cross-language allowed once either side
crosses the configurable fallback timeout).

Extends cleanly to future modes (Clash Club, Battle Royale, Tournament):
those simply add new `mode` values + group sizes elsewhere — the pairing
loop itself only ever reasons about 2-player exact-bucket compatibility,
which is mode-agnostic.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.models.admin import PlatformSettings
from app.models.competitive import CompetitiveQueueEntry, CompetitiveStatistics
from app.services.competitive import blocking_service, match_service, question_engine, queue_event_service
from app.services.notification_engine import emit

logger = logging.getLogger(__name__)

#: Smurf-detection thresholds — architecture only (spec: "do NOT implement
#: punishments yet"), so this only ever LOGS a signal event for a future
#: admin/trust-and-safety phase to act on. Every input is a real, already-
#: tracked CompetitiveStatistics field — nothing fabricated.
_SMURF_MIN_SAMPLE_MATCHES = 5
_SMURF_WIN_RATE_THRESHOLD = 0.85
_SMURF_ACCURACY_THRESHOLD = 95.0
_SMURF_FAST_RESPONSE_MS = 1500


def _get_settings(db: Session) -> PlatformSettings:
    return db.get(PlatformSettings, True) or PlatformSettings()


def update_search_radius(entry: CompetitiveQueueEntry, settings: PlatformSettings) -> int:
    """Progressive radius expansion: 0 (exact rating only) until the first
    configured tier elapses, then the widest tier whose threshold has been
    reached (parallel expansion_seconds/expansion_radius arrays, e.g.
    20s->±50, 40s->±100, 60s->±150)."""
    elapsed = (datetime.now(timezone.utc) - entry.search_started_at).total_seconds()
    radius = 0
    for sec, rad in zip(settings.competitive_mmr_expansion_seconds, settings.competitive_mmr_expansion_radius):
        if elapsed >= sec:
            radius = rad
    return radius


def _win_rate(stats: Optional[CompetitiveStatistics]) -> Optional[float]:
    if stats is None or stats.matches_played <= 0:
        return None
    return stats.wins / stats.matches_played


def _language_compatible(a: CompetitiveQueueEntry, b: CompetitiveQueueEntry, settings: PlatformSettings) -> bool:
    if not a.language or not b.language or a.language == b.language:
        return True
    fallback = settings.competitive_language_fallback_seconds
    elapsed_a = (datetime.now(timezone.utc) - a.search_started_at).total_seconds()
    elapsed_b = (datetime.now(timezone.utc) - b.search_started_at).total_seconds()
    return elapsed_a >= fallback or elapsed_b >= fallback


def compute_match_quality(
    a: CompetitiveQueueEntry,
    b: CompetitiveQueueEntry,
    *,
    stats_a: Optional[CompetitiveStatistics],
    stats_b: Optional[CompetitiveStatistics],
    question_pool_ok: bool,
) -> Dict[str, float]:
    """Weighted 0-100 quality score across real, currently-available data
    dimensions only. No latency/ping dimension: no real ping-measurement
    infrastructure exists anywhere in the codebase, and fabricating one
    would violate "no mocked data"."""
    rating_diff = abs(a.rating_at_join - b.rating_at_join)
    rating_score = max(0.0, 100.0 - (rating_diff / 3.0))  # -1pt per 3 rating points apart

    language_score = 100.0 if (a.language and b.language and a.language == b.language) else 60.0

    wr_a, wr_b = _win_rate(stats_a), _win_rate(stats_b)
    if wr_a is None or wr_b is None:
        win_rate_score = 80.0  # not enough history yet on one side — neutral, not penalized
    else:
        win_rate_score = max(0.0, 100.0 - abs(wr_a - wr_b) * 100.0)

    rt_a = stats_a.avg_response_time_ms if stats_a else None
    rt_b = stats_b.avg_response_time_ms if stats_b else None
    if rt_a is None or rt_b is None:
        response_time_score = 80.0
    else:
        response_time_score = max(0.0, 100.0 - abs(rt_a - rt_b) / 50.0)  # -1pt per 50ms apart

    overall = (
        rating_score * 0.40
        + language_score * 0.20
        + win_rate_score * 0.20
        + response_time_score * 0.20
    )
    if not question_pool_ok:
        overall = 0.0

    return {
        "rating_score": round(rating_score, 2),
        "language_score": round(language_score, 2),
        "win_rate_score": round(win_rate_score, 2),
        "response_time_score": round(response_time_score, 2),
        "question_pool_ok": question_pool_ok,
        "overall_score": round(overall, 2),
    }


def log_smurf_signal_if_any(db: Session, entry: CompetitiveQueueEntry, stats: Optional[CompetitiveStatistics]) -> None:
    """Detection only — architecture for a future trust-and-safety phase.
    Never blocks, delays, or otherwise affects this player's matchmaking."""
    if stats is None or stats.matches_played < _SMURF_MIN_SAMPLE_MATCHES:
        return
    wr = _win_rate(stats)
    reasons: List[str] = []
    if wr is not None and wr >= _SMURF_WIN_RATE_THRESHOLD:
        reasons.append("high_win_rate")
    if stats.accuracy_pct >= _SMURF_ACCURACY_THRESHOLD:
        reasons.append("abnormal_accuracy")
    if stats.avg_response_time_ms is not None and stats.avg_response_time_ms <= _SMURF_FAST_RESPONSE_MS:
        reasons.append("extremely_fast_answers")
    if reasons:
        queue_event_service.log_event(
            db, user_id=entry.user_id, queue_entry_id=entry.id, event_type="smurf_signal_detected",
            meta={"reasons": reasons, "win_rate": wr, "rating": entry.rating_at_join},
        )


def _candidate_pool(db: Session, entry: CompetitiveQueueEntry) -> List[CompetitiveQueueEntry]:
    blocked_ids = set(blocking_service.blocked_user_ids(db, user_id=entry.user_id))
    rows = db.exec(
        select(CompetitiveQueueEntry)
        .where(CompetitiveQueueEntry.status == "searching")
        .where(CompetitiveQueueEntry.mode == entry.mode)
        .where(CompetitiveQueueEntry.subject_id == entry.subject_id)
        .where(CompetitiveQueueEntry.school_level_id == entry.school_level_id)
        .where(CompetitiveQueueEntry.difficulty == entry.difficulty)
        .where(CompetitiveQueueEntry.user_id != entry.user_id)
        .order_by(CompetitiveQueueEntry.search_started_at.asc())
    ).all()
    return [r for r in rows if r.user_id not in blocked_ids]


def find_best_candidate(
    db: Session, entry: CompetitiveQueueEntry, settings: PlatformSettings,
    *, question_pool_ok: bool,
) -> Optional[Tuple[CompetitiveQueueEntry, Dict[str, float]]]:
    best: Optional[Tuple[CompetitiveQueueEntry, Dict[str, float]]] = None
    for candidate in _candidate_pool(db, entry):
        rating_diff = abs(entry.rating_at_join - candidate.rating_at_join)
        allowed_radius = max(entry.current_radius, candidate.current_radius)
        if rating_diff > allowed_radius:
            continue
        if not _language_compatible(entry, candidate, settings):
            continue

        stats_a = db.get(CompetitiveStatistics, entry.user_id)
        stats_b = db.get(CompetitiveStatistics, candidate.user_id)
        quality = compute_match_quality(
            entry, candidate, stats_a=stats_a, stats_b=stats_b, question_pool_ok=question_pool_ok,
        )
        if quality["overall_score"] < settings.competitive_min_match_quality_score:
            continue
        if best is None or quality["overall_score"] > best[1]["overall_score"]:
            best = (candidate, quality)
    return best


def _create_proposal(db: Session, a: CompetitiveQueueEntry, b: CompetitiveQueueEntry, settings: PlatformSettings) -> None:
    group_id = uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.competitive_matchmaking_accept_seconds)

    for one, other in ((a, b), (b, a)):
        one.status = "matched"
        one.proposal_group_id = group_id
        one.matched_with_user_id = other.user_id
        one.proposal_expires_at = expires_at
        one.updated_at = datetime.now(timezone.utc)
        db.add(one)
    db.commit()

    for one, other in ((a, b), (b, a)):
        queue_event_service.log_event(
            db, user_id=one.user_id, queue_entry_id=one.id, event_type="queue_matched",
            meta={"proposal_group_id": str(group_id), "opponent_id": str(other.user_id)},
        )
        emit(
            db,
            event_type="competitive_queue_opponent_found",
            user_id=one.user_id,
            context={},
            data={"queue_entry_id": str(one.id), "proposal_group_id": str(group_id), "expires_at": expires_at.isoformat()},
            dedup_key=f"competitive_queue_opponent_found:{group_id}:{one.user_id}",
        )


def run_matchmaking_sweep(db: Session) -> int:
    """Called by the periodic Celery task (see app/workers/competitive_tasks.py).
    Processes every actively 'searching' entry oldest-first (fairness: the
    longest-waiting players get first pick of a compatible opponent found
    this pass), pairing greedily by highest match-quality score. Not a
    globally-optimal assignment (that would need a full matching algorithm
    recomputed every tick) — a fine trade-off given matches pair only within
    an identical subject/level/difficulty bucket, so bucket sizes stay small.
    Returns the number of proposals created this sweep."""
    settings = _get_settings(db)
    entries = list(
        db.exec(
            select(CompetitiveQueueEntry)
            .where(CompetitiveQueueEntry.status == "searching")
            .order_by(CompetitiveQueueEntry.search_started_at.asc())
        ).all()
    )

    already_paired: set = set()
    pool_ok_cache: Dict[Tuple[UUID, UUID, str], bool] = {}
    proposals_created = 0

    for entry in entries:
        if entry.id in already_paired:
            continue

        entry.current_radius = update_search_radius(entry, settings)
        db.add(entry)
        db.commit()

        pool_key = (entry.subject_id, entry.school_level_id, entry.difficulty)
        if pool_key not in pool_ok_cache:
            available = question_engine.count_available_questions(
                db, subject_ids=[entry.subject_id], school_level_id=entry.school_level_id, difficulty=entry.difficulty,
            )
            default_count = min(settings.competitive_question_count_options) if settings.competitive_question_count_options else 5
            pool_ok_cache[pool_key] = available >= default_count
        question_pool_ok = pool_ok_cache[pool_key]

        stats = db.get(CompetitiveStatistics, entry.user_id)
        log_smurf_signal_if_any(db, entry, stats)

        if entry.id in already_paired:
            continue
        match = find_best_candidate(db, entry, settings, question_pool_ok=question_pool_ok)
        if match is None:
            continue
        candidate, _quality = match
        if candidate.id in already_paired:
            continue

        _create_proposal(db, entry, candidate, settings)
        already_paired.add(entry.id)
        already_paired.add(candidate.id)
        proposals_created += 1

    return proposals_created


def _default_question_count(settings: PlatformSettings) -> int:
    return min(settings.competitive_question_count_options) if settings.competitive_question_count_options else 5


def _lock_proposal_group(db: Session, group_id: UUID) -> List[CompetitiveQueueEntry]:
    """Locks both rows of a proposal group in a single statement, ordered
    by id — this is what makes accept/decline race-safe: two concurrent
    requests touching the same group always request the row lock in the
    same order, so they serialize instead of deadlocking, and the second
    request always sees the first request's committed result."""
    return list(
        db.exec(
            select(CompetitiveQueueEntry)
            .where(CompetitiveQueueEntry.proposal_group_id == group_id)
            .order_by(CompetitiveQueueEntry.id)
            .with_for_update()
        ).all()
    )


def accept_proposal(db: Session, entry: CompetitiveQueueEntry, *, user_id: UUID) -> CompetitiveQueueEntry:
    if entry.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cette proposition ne t'est pas destinée.")
    if entry.proposal_group_id is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Aucune proposition de match en attente.")

    group = _lock_proposal_group(db, entry.proposal_group_id)
    mine = next((e for e in group if e.user_id == user_id), None)
    other = next((e for e in group if e.user_id != user_id), None)
    if mine is None or mine.status != "matched":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Aucune proposition de match en attente.")
    if mine.proposal_expires_at and mine.proposal_expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cette proposition a expiré.")

    now = datetime.now(timezone.utc)
    mine.status = "accepted"
    mine.accepted_at = now
    mine.updated_at = now
    db.add(mine)
    db.commit()
    db.refresh(mine)

    queue_event_service.log_event(db, user_id=user_id, queue_entry_id=mine.id, event_type="queue_accepted", meta={})

    if other is None or other.status != "accepted":
        return mine  # waiting on the opponent's own response

    settings = _get_settings(db)
    default_count = _default_question_count(settings)
    available = question_engine.count_available_questions(
        db, subject_ids=[mine.subject_id], school_level_id=mine.school_level_id, difficulty=mine.difficulty,
    )
    if available < default_count:
        # Pool dried up between proposal and both-accept (extremely rare) —
        # fail safe by returning both to searching rather than creating a
        # match that can't actually draw questions.
        now2 = datetime.now(timezone.utc)
        for e in (mine, other):
            e.status = "searching"
            e.proposal_group_id = None
            e.matched_with_user_id = None
            e.proposal_expires_at = None
            e.accepted_at = None
            e.updated_at = now2
            db.add(e)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Plus assez de questions disponibles pour ce match — retour en recherche.",
        )

    match = match_service.create_matched_pair(
        db,
        player_a_id=mine.user_id,
        player_b_id=other.user_id,
        subject_id=mine.subject_id,
        school_level_id=mine.school_level_id,
        difficulty=mine.difficulty,
        question_count=default_count,
        is_ranked=(mine.mode == "ranked"),
    )
    now3 = datetime.now(timezone.utc)
    for e in (mine, other):
        e.match_id = match.id
        e.ended_at = now3
        e.updated_at = now3
        db.add(e)
    db.commit()
    db.refresh(mine)

    for e in (mine, other):
        queue_event_service.log_event(
            db, user_id=e.user_id, queue_entry_id=e.id, event_type="queue_match_created",
            meta={"match_id": str(match.id)},
        )
    return mine


def decline_proposal(db: Session, entry: CompetitiveQueueEntry, *, user_id: UUID) -> CompetitiveQueueEntry:
    if entry.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cette proposition ne t'est pas destinée.")
    if entry.proposal_group_id is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Aucune proposition de match en attente.")

    group = _lock_proposal_group(db, entry.proposal_group_id)
    mine = next((e for e in group if e.user_id == user_id), None)
    other = next((e for e in group if e.user_id != user_id), None)
    if mine is None or mine.status not in ("matched", "accepted"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Aucune proposition de match en attente.")
    if mine.match_id is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Le match a déjà été créé — impossible de refuser.")

    now = datetime.now(timezone.utc)
    mine.status = "declined"
    mine.ended_at = now
    mine.updated_at = now
    db.add(mine)

    if other is not None and other.status in ("matched", "accepted") and other.match_id is None:
        other.status = "searching"
        other.proposal_group_id = None
        other.matched_with_user_id = None
        other.proposal_expires_at = None
        other.accepted_at = None
        other.updated_at = now
        db.add(other)

    db.commit()
    db.refresh(mine)

    queue_event_service.log_event(db, user_id=user_id, queue_entry_id=mine.id, event_type="queue_declined", meta={})
    if other is not None:
        queue_event_service.log_event(
            db, user_id=other.user_id, queue_entry_id=other.id, event_type="queue_returned_to_search",
            meta={"reason": "opponent_declined"},
        )
        emit(
            db, event_type="competitive_queue_opponent_declined", user_id=other.user_id,
            context={}, data={"queue_entry_id": str(other.id)},
            dedup_key=f"competitive_queue_declined:{entry.proposal_group_id}",
        )
    return mine


def expire_stale_proposals(db: Session) -> int:
    """Resolves proposals whose accept-countdown has elapsed without both
    sides accepting — called every tick by the periodic sweep, alongside
    run_matchmaking_sweep. Whoever DID accept in time goes back to
    'searching' (never penalized for a silent partner, keeping their
    original search_started_at so their radius keeps expanding); whoever
    never responded is marked 'expired' (an explicit re-join is required —
    same posture as a walked-away/AFK player). If NEITHER responded, both
    simply resume searching — nobody is "at fault" for mutual silence
    (spec: "never silently stop searching")."""
    now = datetime.now(timezone.utc)
    stale_group_ids = list(
        db.exec(
            select(CompetitiveQueueEntry.proposal_group_id)
            .where(CompetitiveQueueEntry.status == "matched")
            .where(CompetitiveQueueEntry.proposal_expires_at.is_not(None))
            .where(CompetitiveQueueEntry.proposal_expires_at <= now)
            .distinct()
        ).all()
    )

    resolved = 0
    for group_id in stale_group_ids:
        if group_id is None:
            continue
        group = _lock_proposal_group(db, group_id)
        now2 = datetime.now(timezone.utc)
        accepted = [e for e in group if e.status == "accepted"]
        silent = [e for e in group if e.status == "matched" and e.proposal_expires_at and e.proposal_expires_at <= now2]
        if not silent:
            continue  # already resolved by a concurrent accept/decline between the scan and the lock

        for e in accepted:
            e.status = "searching"
            e.proposal_group_id = None
            e.matched_with_user_id = None
            e.proposal_expires_at = None
            e.accepted_at = None
            e.updated_at = now2
            db.add(e)
            queue_event_service.log_event(
                db, user_id=e.user_id, queue_entry_id=e.id, event_type="queue_returned_to_search",
                meta={"reason": "opponent_timed_out"},
            )
            emit(
                db, event_type="competitive_queue_opponent_declined", user_id=e.user_id,
                context={}, data={"queue_entry_id": str(e.id)},
                dedup_key=f"competitive_queue_timeout:{group_id}:{e.user_id}",
            )

        for e in silent:
            if accepted:
                e.status = "expired"
                e.ended_at = now2
            else:
                e.status = "searching"
                e.proposal_group_id = None
                e.matched_with_user_id = None
                e.proposal_expires_at = None
            e.updated_at = now2
            db.add(e)
            queue_event_service.log_event(
                db, user_id=e.user_id, queue_entry_id=e.id, event_type="queue_proposal_expired",
                meta={"had_accepted_opponent": bool(accepted)},
            )
            if not accepted:
                emit(
                    db, event_type="competitive_queue_expired", user_id=e.user_id,
                    context={}, data={"queue_entry_id": str(e.id)},
                    dedup_key=f"competitive_queue_both_timeout:{group_id}:{e.user_id}",
                )

        db.commit()
        resolved += 1

    return resolved
