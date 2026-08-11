from __future__ import annotations

"""
Event Manager — Competitive Arena Phase 15 (LiveOps).

ArenaEvent's `event_type` discriminates five spec modules onto ONE table:
- limited_time / educational_campaign: objectives are ordinary arena_
  missions rows (period='event', event_id=this event) — the Mission
  Engine tracks their progress, this service only tracks participation +
  an aggregate per-participant "score" (sum of contributions to any of
  the event's objective metric_keys) for the Event Leaderboard.
- community_challenge: config={"metric_key": ..., "target_value": ...}.
  ONE shared arena_community_progress row, incremented atomically on every
  matching record_event() call from ANY participant. "Everyone receives
  rewards" (spec) is interpreted as every user who actually contributed
  (has an arena_event_participants row) — rewarding the entire platform
  regardless of participation would be both unbackable by any real signal
  and contrary to the spec's own Anti-Abuse section.
- happy_hour / double_rewards: config={"multiplier": 2.0, "applies_to":
  ["ep","arena_xp"]}. No objectives, no participants — get_active_
  multiplier() is the sole hook, called from reward_service.grant_reward
  at the one existing reward choke-point (no per-call-site changes
  needed anywhere else, same trick Phase 14 used for cosmetic rewards).
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

import sqlalchemy as sa
from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.models.admin import PlatformSettings
from app.models.liveops import ArenaCommunityProgress, ArenaEvent, ArenaEventParticipant, ArenaMission
from app.models.profile import Profile
from app.services.notification_engine import emit

logger = logging.getLogger(__name__)

EVENT_TYPES = ["limited_time", "community_challenge", "happy_hour", "double_rewards", "educational_campaign"]


def _settings(db: Session) -> PlatformSettings:
    return db.get(PlatformSettings, True) or PlatformSettings()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get_event_or_404(db: Session, event_id: UUID) -> ArenaEvent:
    event = db.get(ArenaEvent, event_id)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Événement introuvable.")
    return event


def list_events(db: Session, *, statuses: Optional[List[str]] = None, event_type: Optional[str] = None) -> List[ArenaEvent]:
    query = select(ArenaEvent)
    if statuses:
        query = query.where(ArenaEvent.status.in_(statuses))
    if event_type:
        query = query.where(ArenaEvent.event_type == event_type)
    return list(db.exec(query.order_by(ArenaEvent.starts_at.desc())).all())


# ─── Admin CRUD / lifecycle ─────────────────────────────────────────────────

def create_event(db: Session, *, created_by: UUID, **fields) -> ArenaEvent:
    if fields.get("event_type") not in EVENT_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="event_type invalide.")
    if fields.get("ends_at") and fields.get("starts_at") and fields["ends_at"] <= fields["starts_at"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="ends_at doit être après starts_at.")
    event = ArenaEvent(created_by=created_by, **fields)
    db.add(event)
    db.commit()
    db.refresh(event)

    if event.event_type == "community_challenge":
        target = int((event.config or {}).get("target_value") or 0)
        db.add(ArenaCommunityProgress(event_id=event.id, target_value=target))
        db.commit()
    logger.info("arena_event created: id=%s type=%s code=%s", event.id, event.event_type, event.code)
    return event


def update_event(db: Session, event_id: UUID, **fields) -> ArenaEvent:
    event = get_event_or_404(db, event_id)
    for key, value in fields.items():
        if value is not None and hasattr(event, key):
            setattr(event, key, value)
    event.updated_at = _now()
    db.add(event)
    db.commit()
    db.refresh(event)

    if event.event_type == "community_challenge" and "config" in fields and fields["config"]:
        progress = db.get(ArenaCommunityProgress, event.id)
        target = int((event.config or {}).get("target_value") or 0)
        if progress is None:
            db.add(ArenaCommunityProgress(event_id=event.id, target_value=target))
        else:
            progress.target_value = target
            db.add(progress)
        db.commit()
    return event


def schedule_event(db: Session, event_id: UUID) -> ArenaEvent:
    event = get_event_or_404(db, event_id)
    if event.status not in ("draft", "scheduled"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cet événement ne peut plus être planifié.")
    event.status = "scheduled"
    event.updated_at = _now()
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def cancel_event(db: Session, event_id: UUID) -> ArenaEvent:
    event = get_event_or_404(db, event_id)
    if event.status in ("ended", "cancelled", "archived"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cet événement est déjà terminé.")
    event.status = "cancelled"
    event.updated_at = _now()
    db.add(event)
    db.commit()
    db.refresh(event)
    logger.info("arena_event cancelled: id=%s", event.id)
    return event


def archive_event(db: Session, event_id: UUID) -> ArenaEvent:
    event = get_event_or_404(db, event_id)
    event.status = "archived"
    event.updated_at = _now()
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def activate_due_events(db: Session) -> int:
    now = _now()
    due = db.exec(
        select(ArenaEvent).where(ArenaEvent.status == "scheduled").where(ArenaEvent.starts_at <= now).where(ArenaEvent.ends_at > now)
    ).all()
    for event in due:
        event.status = "active"
        event.updated_at = now
        db.add(event)
        db.commit()

        event_type_map = {
            "happy_hour": "arena_happy_hour_started",
            "double_rewards": "arena_double_xp_started",
        }
        notif_type = event_type_map.get(event.event_type, "arena_event_started")
        for user_id in db.exec(select(Profile.id)).all():
            emit(
                db, event_type=notif_type, user_id=user_id,
                context={"event_name": event.name}, data={"event_id": str(event.id)},
                dedup_key=f"{notif_type}:{event.id}:{user_id}",
            )
        logger.info("arena_event activated: id=%s type=%s", event.id, event.event_type)
    return len(due)


def end_expired_events(db: Session) -> int:
    now = _now()
    due = db.exec(select(ArenaEvent).where(ArenaEvent.status == "active").where(ArenaEvent.ends_at <= now)).all()
    for event in due:
        event.status = "ended"
        event.updated_at = now
        db.add(event)
        db.commit()
        logger.info("arena_event ended: id=%s type=%s", event.id, event.event_type)
    return len(due)


def notify_events_ending_soon(db: Session) -> int:
    settings_row = _settings(db)
    window = timedelta(hours=settings_row.competitive_event_ending_soon_hours)
    now = _now()
    due = db.exec(
        select(ArenaEvent).where(ArenaEvent.status == "active").where(ArenaEvent.ends_at <= now + window).where(ArenaEvent.ends_at > now)
    ).all()
    sent = 0
    for event in due:
        participant_ids = db.exec(select(ArenaEventParticipant.user_id).where(ArenaEventParticipant.event_id == event.id)).all()
        for user_id in participant_ids:
            result = emit(
                db, event_type="arena_event_ending_soon", user_id=user_id,
                context={"event_name": event.name}, data={"event_id": str(event.id)},
                dedup_key=f"arena_event_ending_soon:{event.id}:{user_id}",
            )
            if result is not None:
                sent += 1
    return sent


def notify_happy_hour_starting_soon(db: Session) -> int:
    settings_row = _settings(db)
    window = timedelta(minutes=settings_row.competitive_happy_hour_starting_soon_minutes)
    now = _now()
    due = db.exec(
        select(ArenaEvent)
        .where(ArenaEvent.status == "scheduled")
        .where(ArenaEvent.event_type.in_(["happy_hour", "double_rewards"]))
        .where(ArenaEvent.starts_at <= now + window)
        .where(ArenaEvent.starts_at > now)
    ).all()
    sent = 0
    for event in due:
        for user_id in db.exec(select(Profile.id)).all():
            result = emit(
                db, event_type="arena_happy_hour_starting_soon", user_id=user_id,
                context={"event_name": event.name}, data={"event_id": str(event.id)},
                dedup_key=f"arena_happy_hour_starting_soon:{event.id}",
            )
            if result is not None:
                sent += 1
    return sent


# ─── Player-facing: join / contribute / leaderboard / claim ───────────────

def _get_or_create_participant(db: Session, event_id: UUID, user_id: UUID) -> ArenaEventParticipant:
    row = db.exec(
        select(ArenaEventParticipant).where(ArenaEventParticipant.event_id == event_id).where(ArenaEventParticipant.user_id == user_id)
    ).first()
    if row is not None:
        return row
    row = ArenaEventParticipant(event_id=event_id, user_id=user_id)
    db.add(row)
    try:
        db.commit()
    except Exception:
        db.rollback()
        row = db.exec(
            select(ArenaEventParticipant).where(ArenaEventParticipant.event_id == event_id).where(ArenaEventParticipant.user_id == user_id)
        ).first()
        return row
    db.refresh(row)
    return row


def join_event(db: Session, user_id: UUID, event_id: UUID) -> ArenaEventParticipant:
    event = get_event_or_404(db, event_id)
    if event.status not in ("active", "scheduled"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cet événement n'accepte plus d'inscriptions.")
    return _get_or_create_participant(db, event_id, user_id)


def get_active_multiplier(db: Session, reward_type: str) -> float:
    """Reward Distributor hook — reward_service.grant_reward calls this for
    every ep/arena_xp grant. Returns 1.0 (no-op) unless a happy_hour/
    double_rewards event is currently active and its config.applies_to
    includes this reward_type."""
    now = _now()
    events = db.exec(
        select(ArenaEvent)
        .where(ArenaEvent.status == "active")
        .where(ArenaEvent.event_type.in_(["happy_hour", "double_rewards"]))
        .where(ArenaEvent.starts_at <= now).where(ArenaEvent.ends_at > now)
    ).all()
    best = 1.0
    for event in events:
        applies_to = (event.config or {}).get("applies_to") or []
        if reward_type in applies_to:
            best = max(best, float((event.config or {}).get("multiplier") or 1.0))
    return best


def record_event(db: Session, *, user_id: UUID, metric_key: str, amount: float = 1) -> None:
    now = _now()
    active_events = db.exec(
        select(ArenaEvent).where(ArenaEvent.status == "active").where(ArenaEvent.starts_at <= now).where(ArenaEvent.ends_at > now)
    ).all()
    if not active_events:
        return

    for event in active_events:
        if event.event_type == "community_challenge":
            if (event.config or {}).get("metric_key") != metric_key:
                continue
            progress = db.get(ArenaCommunityProgress, event.id)
            if progress is None or progress.completed_at is not None:
                continue
            participant = _get_or_create_participant(db, event.id, user_id)
            contributed = dict(participant.progress or {})
            contributed["contributed"] = contributed.get("contributed", 0) + amount
            participant.progress = contributed
            db.add(participant)

            db.exec(
                sa.update(ArenaCommunityProgress)
                .where(ArenaCommunityProgress.event_id == event.id)
                .values(current_value=ArenaCommunityProgress.current_value + int(amount), updated_at=now)
            )
            db.commit()
            db.refresh(progress)

            if progress.current_value >= progress.target_value and progress.completed_at is None:
                _complete_community_challenge(db, event, progress)

        elif event.event_type in ("limited_time", "educational_campaign"):
            objective_keys = set(db.exec(
                select(ArenaMission.metric_key).where(ArenaMission.event_id == event.id)
            ).all())
            if metric_key not in objective_keys:
                continue
            participant = _get_or_create_participant(db, event.id, user_id)
            prog = dict(participant.progress or {})
            prog["score"] = prog.get("score", 0) + amount
            participant.progress = prog
            db.add(participant)
            db.commit()


def _complete_community_challenge(db: Session, event: ArenaEvent, progress: ArenaCommunityProgress) -> None:
    progress.completed_at = _now()
    db.add(progress)
    db.commit()
    logger.info("arena community challenge completed: event=%s", event.id)

    from app.services.competitive import reward_service
    participants = db.exec(select(ArenaEventParticipant).where(ArenaEventParticipant.event_id == event.id)).all()
    for participant in participants:
        for entry in (event.reward_config or []):
            reward_type = entry.get("reward_type")
            if not reward_type:
                continue
            try:
                reward_service.grant_reward(
                    db, user_id=participant.user_id, season_id=None, source=f"community_challenge:{event.id}",
                    reward_type=reward_type, reward_ref=entry.get("reward_ref"), reward_amount=entry.get("reward_amount"),
                    notify=False,
                )
            except Exception:
                logger.exception("community challenge reward grant failed user=%s event=%s", participant.user_id, event.id)
        emit(
            db, event_type="arena_community_goal_completed", user_id=participant.user_id,
            context={"event_name": event.name}, data={"event_id": str(event.id)},
            dedup_key=f"arena_community_goal_completed:{event.id}:{participant.user_id}",
        )


def claim_event_reward(db: Session, user_id: UUID, event_id: UUID) -> Dict[str, Any]:
    event = get_event_or_404(db, event_id)
    # Phase 16 — row-level lock: closes the double-claim race window
    # between two simultaneous requests (same rationale as mission_service.
    # claim_mission).
    participant = db.exec(
        select(ArenaEventParticipant)
        .where(ArenaEventParticipant.event_id == event_id).where(ArenaEventParticipant.user_id == user_id)
        .with_for_update()
    ).first()
    if participant is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tu ne participes pas à cet événement.")
    if participant.reward_claimed_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Récompense déjà réclamée.")

    if event.event_type == "community_challenge":
        progress = db.get(ArenaCommunityProgress, event_id)
        if progress is None or progress.completed_at is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="L'objectif communautaire n'est pas encore atteint.")
    elif event.status not in ("active", "ended"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cet événement n'est pas actif.")

    # Phase 16 — claim-then-grant, not grant-then-claim: reward_service.
    # grant_reward commits internally (releasing the row lock above at its
    # first commit), so the idempotency flag must be persisted BEFORE any
    # reward is granted, or a tight race could still double-grant between
    # two requests that both passed the check above.
    participant.reward_claimed_at = _now()
    db.add(participant)
    db.commit()

    from app.services.competitive import reward_service
    granted = []
    for entry in (event.reward_config or []):
        reward_type = entry.get("reward_type")
        if not reward_type:
            continue
        grant = reward_service.grant_reward(
            db, user_id=user_id, season_id=None, source=f"event:{event.code}",
            reward_type=reward_type, reward_ref=entry.get("reward_ref"), reward_amount=entry.get("reward_amount"),
            notify=False,
        )
        granted.append({"reward_type": reward_type, "status": grant.status})
    return {"event_id": str(event_id), "rewards": granted}


def get_community_progress(db: Session, event_id: UUID) -> Dict[str, Any]:
    progress = db.get(ArenaCommunityProgress, event_id)
    if progress is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Défi communautaire introuvable.")
    pct = round((progress.current_value / progress.target_value) * 100, 2) if progress.target_value else 0.0
    return {
        "event_id": str(event_id), "current_value": progress.current_value, "target_value": progress.target_value,
        "progress_pct": min(100.0, pct), "completed_at": progress.completed_at, "updated_at": progress.updated_at,
    }


def get_event_leaderboard(
    db: Session, event_id: UUID, *, club_id: Optional[UUID] = None, wilaya: Optional[str] = None,
    school_level_id: Optional[str] = None, page: int = 1, size: int = 50,
) -> Tuple[List[Dict[str, Any]], int]:
    get_event_or_404(db, event_id)
    query = select(ArenaEventParticipant, Profile).join(Profile, Profile.id == ArenaEventParticipant.user_id).where(ArenaEventParticipant.event_id == event_id)

    if club_id is not None:
        from app.models.club import ClubMember
        query = query.join(ClubMember, ClubMember.user_id == ArenaEventParticipant.user_id).where(ClubMember.club_id == club_id).where(ClubMember.status == "active")
    if wilaya:
        query = query.where(Profile.wilaya == wilaya)
    if school_level_id:
        from app.models.profile import StudentProfile
        query = query.join(StudentProfile, StudentProfile.user_id == Profile.id).where(StudentProfile.level_main == school_level_id)

    total = db.exec(select(sa.func.count()).select_from(query.subquery())).one()
    rows = list(db.exec(query.offset((page - 1) * size).limit(size)).all())

    def _score(p: ArenaEventParticipant) -> float:
        prog = p.progress or {}
        return float(prog.get("score") or prog.get("contributed") or 0)

    rows.sort(key=lambda pair: _score(pair[0]), reverse=True)
    items = [
        {
            "rank": (page - 1) * size + idx + 1, "user_id": str(participant.user_id),
            "full_name": profile.full_name, "avatar_url": profile.avatar_url,
            "score": _score(participant), "completed_at": participant.completed_at,
        }
        for idx, (participant, profile) in enumerate(rows)
    ]
    return items, total


def get_event_participation(db: Session, event_id: UUID) -> List[Dict[str, Any]]:
    """Admin monitoring — spec's 'Monitor participation'."""
    rows = db.exec(
        select(ArenaEventParticipant, Profile).join(Profile, Profile.id == ArenaEventParticipant.user_id).where(ArenaEventParticipant.event_id == event_id)
    ).all()
    return [
        {
            "user_id": str(p.user_id), "full_name": profile.full_name, "progress": p.progress,
            "joined_at": p.joined_at, "completed_at": p.completed_at, "reward_claimed_at": p.reward_claimed_at,
        }
        for p, profile in rows
    ]
