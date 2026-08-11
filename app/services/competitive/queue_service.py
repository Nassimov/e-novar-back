from __future__ import annotations

"""
Queue Service — Competitive Arena Phase 6.

Owns a single player's own queue-entry lifecycle (join/leave/status/
reconnect) and the server-side Duplicate Queue / Multi-Device protections
("one account, one matchmaking session" — spec). The partial unique index
uq_queue_entries_active_user (migration 079) is the hard guarantee; this
service's own pre-check exists purely to return a clean 409 instead of a
raw IntegrityError on the rare race between two simultaneous joins from the
same account.

Pairing, search-radius expansion, match-quality scoring, and the proposal
accept/decline flow live in matchmaking_service.py — this module never
looks at any OTHER player's queue entry.
"""

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models.admin import PlatformSettings
from app.models.competitive import CompetitiveQueueEntry, CompetitiveStatistics
from app.services.competitive import queue_event_service, statistics_service

#: Queue statuses that count as "an active session already exists" for the
#: Duplicate Queue / Multi-Device Protection check — mirrors the partial
#: unique index's WHERE clause in migration 079 exactly.
ACTIVE_STATUSES = ("waiting", "searching", "matched", "accepted")

#: Statuses a player may still unilaterally leave from. Once paired
#: ("matched"/"accepted"), leaving must go through the proposal
#: decline endpoint instead (matchmaking_service) since a second player is
#: already waiting on this one's answer.
LEAVABLE_STATUSES = ("waiting", "searching")


def _get_settings(db: Session) -> PlatformSettings:
    return db.get(PlatformSettings, True) or PlatformSettings()


def _assert_not_suspended(db: Session, user_id: UUID) -> None:
    stats = db.get(CompetitiveStatistics, user_id)
    if stats and stats.suspended_until and stats.suspended_until > datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Accès à l'Arène Compétitive suspendu pour ce compte.",
        )


def get_active_entry(db: Session, user_id: UUID) -> Optional[CompetitiveQueueEntry]:
    # match_id IS NULL matters: an 'accepted' entry that already produced a
    # real match is a permanent success record, not an ongoing session (see
    # migration 081) — otherwise a player's very first successful match
    # would lock them out of matchmaking forever.
    return db.exec(
        select(CompetitiveQueueEntry)
        .where(CompetitiveQueueEntry.user_id == user_id)
        .where(CompetitiveQueueEntry.status.in_(ACTIVE_STATUSES))
        .where(CompetitiveQueueEntry.match_id.is_(None))
        .order_by(CompetitiveQueueEntry.created_at.desc())
    ).first()


def get_active_entry_or_404(db: Session, user_id: UUID) -> CompetitiveQueueEntry:
    entry = get_active_entry(db, user_id)
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Aucune recherche de match en cours.")
    return entry


def get_entry_or_404(db: Session, entry_id: UUID) -> CompetitiveQueueEntry:
    entry = db.get(CompetitiveQueueEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recherche de match introuvable.")
    return entry


def join_queue(
    db: Session,
    *,
    user_id: UUID,
    mode: str,
    subject_id: UUID,
    school_level_id: UUID,
    difficulty: str,
    language: Optional[str],
) -> CompetitiveQueueEntry:
    if mode not in ("ranked", "friendly"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Mode de matchmaking invalide.")

    _assert_not_suspended(db, user_id)

    if mode == "ranked":
        from app.services.competitive import ranking_service
        ranking_service.check_ranked_eligibility(db, user_id)

    if get_active_entry(db, user_id) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Une recherche de match est déjà en cours sur ce compte.",
        )

    stats = statistics_service.get_or_create_statistics(db, user_id)

    entry = CompetitiveQueueEntry(
        user_id=user_id,
        mode=mode,
        subject_id=subject_id,
        school_level_id=school_level_id,
        difficulty=difficulty,
        language=language,
        status="searching",
        rating_at_join=stats.rating,
        current_radius=0,  # exact-rating only until the first expansion tier elapses
    )
    db.add(entry)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Une recherche de match est déjà en cours sur ce compte.",
        )
    db.refresh(entry)

    queue_event_service.log_event(
        db, user_id=user_id, queue_entry_id=entry.id, event_type="queue_joined",
        meta={"mode": mode, "subject_id": str(subject_id), "difficulty": difficulty},
    )
    return entry


def leave_queue(db: Session, entry: CompetitiveQueueEntry, *, user_id: UUID) -> CompetitiveQueueEntry:
    if entry.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cette recherche ne t'appartient pas.")
    if entry.status not in LEAVABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Un adversaire a déjà été proposé — réponds à la proposition de match au lieu d'annuler.",
        )

    entry.status = "cancelled"
    entry.ended_at = datetime.now(timezone.utc)
    entry.updated_at = datetime.now(timezone.utc)
    db.add(entry)
    db.commit()
    db.refresh(entry)

    queue_event_service.log_event(
        db, user_id=user_id, queue_entry_id=entry.id, event_type="queue_left", meta={},
    )
    return entry


def estimate_wait_seconds(db: Session, entry: CompetitiveQueueEntry) -> int:
    """Real, historical-data-driven estimate: median observed search
    duration for entries that actually got matched in this exact
    mode/subject/level/difficulty bucket over the last 24h. Falls back to
    the admin-configured default when there isn't enough history yet (a
    brand new subject/level combo, or a quiet platform) — never a fake
    hardcoded number presented as if it were real."""
    from datetime import timedelta

    since = datetime.now(timezone.utc) - timedelta(hours=24)
    rows = db.exec(
        select(CompetitiveQueueEntry.search_started_at, CompetitiveQueueEntry.accepted_at)
        .where(CompetitiveQueueEntry.mode == entry.mode)
        .where(CompetitiveQueueEntry.subject_id == entry.subject_id)
        .where(CompetitiveQueueEntry.school_level_id == entry.school_level_id)
        .where(CompetitiveQueueEntry.difficulty == entry.difficulty)
        .where(CompetitiveQueueEntry.status == "accepted")
        .where(CompetitiveQueueEntry.accepted_at.is_not(None))
        .where(CompetitiveQueueEntry.search_started_at >= since)
    ).all()
    durations = [
        (accepted_at - started_at).total_seconds()
        for started_at, accepted_at in rows
        if accepted_at and started_at and accepted_at > started_at
    ]
    if not durations:
        return _get_settings(db).competitive_queue_default_wait_estimate_sec

    durations.sort()
    mid = len(durations) // 2
    median = durations[mid] if len(durations) % 2 else (durations[mid - 1] + durations[mid]) / 2
    return max(5, round(median))
