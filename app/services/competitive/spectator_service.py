from __future__ import annotations

"""
Spectator Service — Competitive Arena Phase 8.

Owns everything a spectator (a connected user who is NOT a participant of
the match they're watching) can do: join/leave bookkeeping, predictions,
reactions (grouped before broadcast — see reactions.py's counterpart Celery
flush task), chat (+ moderation: mutes/blocked-words/reports), bookmarks,
and the lazily-created per-user CompetitiveSpectatorStats row (mirrors
statistics_service.get_or_create_statistics's exact pattern).

"Arena XP"/"Spectator XP" are an intentional stub currency — simple integer
counters, NEVER Arena Rating (MMR) and NEVER EP (spec: "Never grant Arena
Rating. Never grant EP."). No redemption economy/shop exists on top of them
yet, matching Phase 7's reward_service 'recorded_only' philosophy for grant
types with no consumption system built yet.
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from uuid import UUID

from fastapi import HTTPException, status
from sqlmodel import Session, func, select

from app.core.redis import get_redis_client
from app.core.spectator_ws import publish_spectator_event
from app.models.admin import PlatformSettings
from app.models.competitive import (
    REACTION_EMOJI_SET,
    CompetitiveBlockedWord,
    CompetitiveChatMute,
    CompetitiveChatReport,
    CompetitiveMatch,
    CompetitiveMatchBookmark,
    CompetitiveMatchChat,
    CompetitiveMatchReaction,
    CompetitivePrediction,
    CompetitiveSpectator,
    CompetitiveSpectatorStats,
)
from app.models.profile import Profile
from app.services.competitive import event_log_service, match_service

logger = logging.getLogger(__name__)


def _settings(db: Session) -> PlatformSettings:
    return db.get(PlatformSettings, True) or PlatformSettings()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─── Spectator stats (lazily created, mirrors statistics_service) ──────────

def get_or_create_spectator_stats(db: Session, user_id: UUID) -> CompetitiveSpectatorStats:
    stats = db.get(CompetitiveSpectatorStats, user_id)
    if stats is not None:
        return stats
    stats = CompetitiveSpectatorStats(user_id=user_id)
    db.add(stats)
    db.commit()
    db.refresh(stats)
    return stats


# ─── Join / leave bookkeeping ───────────────────────────────────────────────

def record_spectator_join(db: Session, *, match_id: UUID, user_id: UUID) -> None:
    """Always writes a fresh session row (durable audit trail — see
    CompetitiveSpectator's docstring); only bumps matches_watched the FIRST
    time this user has ever spectated this particular match (so a reconnect
    never double-counts "matches watched")."""
    already_watched = db.exec(
        select(CompetitiveSpectator)
        .where(CompetitiveSpectator.match_id == match_id)
        .where(CompetitiveSpectator.user_id == user_id)
    ).first()

    db.add(CompetitiveSpectator(match_id=match_id, user_id=user_id))
    stats = get_or_create_spectator_stats(db, user_id)
    if already_watched is None:
        stats.matches_watched += 1
        stats.updated_at = _now()
        db.add(stats)
    db.commit()

    event_log_service.log_event(db, match_id=match_id, actor_id=user_id, event_type="spectator_join", meta={})


def record_spectator_leave(db: Session, *, match_id: UUID, user_id: UUID) -> None:
    row = db.exec(
        select(CompetitiveSpectator)
        .where(CompetitiveSpectator.match_id == match_id)
        .where(CompetitiveSpectator.user_id == user_id)
        .where(CompetitiveSpectator.left_at.is_(None))
        .order_by(CompetitiveSpectator.joined_at.desc())
    ).first()
    if row is not None:
        row.left_at = _now()
        db.add(row)
        db.commit()

    event_log_service.log_event(db, match_id=match_id, actor_id=user_id, event_type="spectator_leave", meta={})


# ─── Rate limiting (Phase 16 — shared app/core/rate_limiter.py) ────────────

def _rate_limit(*, key: str, limit: int, window_seconds: int = 10) -> None:
    from app.core.rate_limiter import check_rate_limit
    check_rate_limit(key=key, limit=limit, window_seconds=window_seconds)


# ─── Reactions (grouped before broadcast) ───────────────────────────────────

#: How long the grouping window stays open before a single flush task
#: collapses every reaction of the same emoji into one broadcast — a
#: pragmatic minimal version of "grouped automatically, do not flood the
#: interface": the FIRST reaction of a (match, emoji) burst schedules a
#: Celery flush countdown seconds later (reusing this codebase's existing
#: "dispatch a delayed Celery task" convention, e.g.
#: task_check_waiting_room_disconnect) which reads however many accumulated
#: in the meantime from a short-lived Redis counter and broadcasts ONE
#: grouped event. Every reaction after the first in the same window just
#: increments the counter — no additional task, no additional broadcast.
_REACTION_GROUP_WINDOW_SECONDS = 0.6


def _reaction_group_key(match_id: UUID, emoji: str) -> str:
    return f"competitive:reaction_group:{match_id}:{emoji}"


def create_reaction(db: Session, match: CompetitiveMatch, *, user_id: UUID, emoji: str) -> None:
    if emoji not in REACTION_EMOJI_SET:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Émoji non supporté.")

    settings_row = _settings(db)
    _rate_limit(
        key=f"competitive:reaction_rate:{match.id}:{user_id}",
        limit=settings_row.competitive_reaction_rate_limit_per_10s,
    )

    db.add(CompetitiveMatchReaction(match_id=match.id, user_id=user_id, emoji=emoji))
    stats = get_or_create_spectator_stats(db, user_id)
    stats.reactions_sent += 1
    stats.updated_at = _now()
    db.add(stats)
    db.commit()

    try:
        r = get_redis_client()
        key = _reaction_group_key(match.id, emoji)
        n = r.incr(key)
        if n == 1:
            r.expire(key, 5)  # safety net if the flush task is ever dropped
            from app.workers.competitive_tasks import task_flush_reaction_group
            task_flush_reaction_group.apply_async(
                args=[str(match.id), emoji], countdown=_REACTION_GROUP_WINDOW_SECONDS,
            )
    except Exception:
        logger.debug("reaction grouping failed match=%s emoji=%s — broadcasting ungrouped", match.id, emoji, exc_info=True)
        publish_spectator_event(str(match.id), {"type": "reaction", "emoji": emoji, "count": 1})


# ─── Predictions ────────────────────────────────────────────────────────────

def _predictable_participants(db: Session, match: CompetitiveMatch) -> Tuple[Optional[UUID], Optional[UUID]]:
    participants = match_service.get_participants(db, match.id)
    player_a = match.creator_id
    player_b = next((p.user_id for p in participants if p.user_id != player_a), None)
    return player_a, player_b


def predictions_are_open(db: Session, match: CompetitiveMatch) -> bool:
    if match.status in ("completed", "cancelled", "expired", "abandoned", "disconnected"):
        return False
    settings_row = _settings(db)
    lock_position = settings_row.competitive_prediction_lock_before_position
    if match.current_question_position is not None and match.current_question_position >= lock_position:
        return False
    return True


def create_or_update_prediction(
    db: Session, match: CompetitiveMatch, *, user_id: UUID, predicted_winner_id: Optional[UUID],
) -> CompetitivePrediction:
    if not predictions_are_open(db, match):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Les pronostics sont fermés pour ce match.")

    if predicted_winner_id is not None:
        player_a, player_b = _predictable_participants(db, match)
        if predicted_winner_id not in (player_a, player_b):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Joueur invalide pour ce match.")

    existing = db.exec(
        select(CompetitivePrediction)
        .where(CompetitivePrediction.match_id == match.id)
        .where(CompetitivePrediction.user_id == user_id)
    ).first()

    if existing is not None:
        existing.predicted_winner_id = predicted_winner_id
        db.add(existing)
        db.commit()
        db.refresh(existing)
        return existing

    prediction = CompetitivePrediction(match_id=match.id, user_id=user_id, predicted_winner_id=predicted_winner_id)
    db.add(prediction)
    stats = get_or_create_spectator_stats(db, user_id)
    stats.predictions_made += 1
    stats.updated_at = _now()
    db.add(stats)
    db.commit()
    db.refresh(prediction)

    event_log_service.log_event(
        db, match_id=match.id, actor_id=user_id, event_type="prediction_made",
        meta={"predicted_winner_id": str(predicted_winner_id) if predicted_winner_id else None},
    )
    publish_spectator_event(str(match.id), {"type": "prediction_summary", "match_id": str(match.id)})
    return prediction


def get_prediction_summary(db: Session, match: CompetitiveMatch, *, viewer_id: Optional[UUID] = None) -> Dict:
    player_a, player_b = _predictable_participants(db, match)
    rows = list(db.exec(select(CompetitivePrediction).where(CompetitivePrediction.match_id == match.id)).all())

    player_a_votes = sum(1 for r in rows if r.predicted_winner_id == player_a)
    player_b_votes = sum(1 for r in rows if player_b is not None and r.predicted_winner_id == player_b)
    draw_votes = sum(1 for r in rows if r.predicted_winner_id is None)

    my_prediction = next((r for r in rows if viewer_id is not None and r.user_id == viewer_id), None)

    return {
        "match_id": match.id,
        "predictions_open": predictions_are_open(db, match),
        "total_predictions": len(rows),
        "player_a_id": player_a,
        "player_a_votes": player_a_votes,
        "player_b_id": player_b,
        "player_b_votes": player_b_votes,
        "draw_votes": draw_votes,
        "my_prediction": my_prediction,
    }


def resolve_predictions_for_match(
    db: Session, *, match_id: UUID, winner_id: Optional[UUID], is_draw: bool,
) -> int:
    """Called once from gameplay_service.finish_match() — the same place
    Phase 7's ranking/reward hooks were wired in. Resolves every UNRESOLVED
    prediction (is_correct IS NULL) for this match: sets is_correct, credits
    Arena XP/Spectator XP (never MMR, never EP) on a correct pick, and
    updates the predictor's aggregate stats row. Returns how many
    predictions were resolved (0 is a normal, expected outcome — most
    matches have no spectators)."""
    settings_row = _settings(db)
    open_predictions = list(
        db.exec(
            select(CompetitivePrediction)
            .where(CompetitivePrediction.match_id == match_id)
            .where(CompetitivePrediction.is_correct.is_(None))
        ).all()
    )
    resolved = 0
    for prediction in open_predictions:
        correct = (is_draw and prediction.predicted_winner_id is None) or (
            not is_draw and winner_id is not None and prediction.predicted_winner_id == winner_id
        )
        prediction.is_correct = correct
        stats = get_or_create_spectator_stats(db, prediction.user_id)
        if correct:
            prediction.xp_awarded = settings_row.competitive_prediction_reward_arena_xp
            stats.predictions_correct += 1
            stats.arena_xp += settings_row.competitive_prediction_reward_arena_xp
            stats.spectator_xp += settings_row.competitive_prediction_reward_spectator_xp
        stats.updated_at = _now()
        db.add(prediction)
        db.add(stats)
        resolved += 1
    if resolved:
        db.commit()
    return resolved


# ─── Chat + moderation ──────────────────────────────────────────────────────

def _is_muted(db: Session, user_id: UUID) -> bool:
    now = _now()
    rows = db.exec(select(CompetitiveChatMute).where(CompetitiveChatMute.user_id == user_id)).all()
    return any(row.muted_until is None or row.muted_until > now for row in rows)


def _contains_blocked_word(db: Session, text: str) -> bool:
    words = db.exec(select(CompetitiveBlockedWord.word)).all()
    lowered = text.lower()
    return any(w.lower() in lowered for w in words)


def assert_can_chat(db: Session, match: CompetitiveMatch, *, user_id: UUID) -> None:
    """Players cannot use spectator chat DURING THEIR OWN MATCH — they can
    obviously chat as spectators in someone else's match."""
    participants = match_service.get_participants(db, match.id)
    if user_id in [p.user_id for p in participants]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Les joueurs ne peuvent pas utiliser le chat spectateur pendant leur propre match.",
        )
    if _is_muted(db, user_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tu es actuellement mis en sourdine sur ce chat.")


def create_chat_message(
    db: Session, match: CompetitiveMatch, *, user_id: UUID, text: str, reply_to_id: Optional[UUID],
) -> CompetitiveMatchChat:
    assert_can_chat(db, match, user_id=user_id)
    if _contains_blocked_word(db, text):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Message refusé : contient un mot interdit.")

    settings_row = _settings(db)
    _rate_limit(
        key=f"competitive:chat_rate:{match.id}:{user_id}",
        limit=settings_row.competitive_chat_rate_limit_per_10s,
    )

    if reply_to_id is not None:
        parent = db.get(CompetitiveMatchChat, reply_to_id)
        if parent is None or parent.match_id != match.id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Message parent introuvable.")

    message = CompetitiveMatchChat(match_id=match.id, user_id=user_id, text=text, reply_to_id=reply_to_id)
    db.add(message)
    stats = get_or_create_spectator_stats(db, user_id)
    stats.chat_messages_sent += 1
    stats.updated_at = _now()
    db.add(stats)
    db.commit()
    db.refresh(message)

    profile = db.get(Profile, user_id)
    publish_spectator_event(str(match.id), {
        "type": "chat_message",
        "id": str(message.id), "match_id": str(match.id), "user_id": str(user_id),
        "full_name": profile.full_name if profile else None,
        "avatar_url": profile.avatar_url if profile else None,
        "text": message.text, "reply_to_id": str(reply_to_id) if reply_to_id else None,
        "created_at": message.created_at.isoformat(),
    })
    event_log_service.log_event(db, match_id=match.id, actor_id=user_id, event_type="chat_message_sent", meta={"message_id": str(message.id)})
    return message


def delete_own_chat_message(db: Session, *, match_id: UUID, message_id: UUID, user_id: UUID) -> CompetitiveMatchChat:
    message = db.get(CompetitiveMatchChat, message_id)
    if message is None or message.match_id != match_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message introuvable.")
    if message.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tu ne peux supprimer que tes propres messages.")
    message.deleted_at = _now()
    message.deleted_by = user_id
    db.add(message)
    db.commit()
    db.refresh(message)
    publish_spectator_event(str(match_id), {"type": "chat_deleted", "message_id": str(message_id)})
    return message


def admin_delete_chat_message(db: Session, *, match_id: UUID, message_id: UUID) -> CompetitiveMatchChat:
    """Admin-only deletion — deleted_by stays None (admins are never
    profiles.id rows, see app/dependencies.py's get_admin_user)."""
    message = db.get(CompetitiveMatchChat, message_id)
    if message is None or message.match_id != match_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message introuvable.")
    message.deleted_at = _now()
    db.add(message)
    db.commit()
    db.refresh(message)
    publish_spectator_event(str(match_id), {"type": "chat_deleted", "message_id": str(message_id)})
    return message


def report_chat_message(db: Session, *, message_id: UUID, reporter_id: UUID, reason: Optional[str]) -> CompetitiveChatReport:
    message = db.get(CompetitiveMatchChat, message_id)
    if message is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message introuvable.")
    report = CompetitiveChatReport(message_id=message_id, reporter_id=reporter_id, reason=reason)
    db.add(report)
    db.commit()
    db.refresh(report)
    event_log_service.log_event(db, match_id=message.match_id, actor_id=reporter_id, event_type="chat_message_reported", meta={"message_id": str(message_id)})
    return report


def list_chat_history(db: Session, match_id: UUID, *, page: int = 1, size: int = 30) -> Tuple[List[Dict], int]:
    page = max(1, page)
    size = max(1, min(size, 100))
    query = select(CompetitiveMatchChat).where(CompetitiveMatchChat.match_id == match_id)
    total = db.exec(select(func.count()).select_from(query.subquery())).one()
    rows = list(
        db.exec(query.order_by(CompetitiveMatchChat.created_at.desc()).offset((page - 1) * size).limit(size)).all()
    )
    profiles = {p.id: p for p in db.exec(select(Profile).where(Profile.id.in_({r.user_id for r in rows}))).all()} if rows else {}
    items = [
        {
            "id": r.id, "match_id": r.match_id, "user_id": r.user_id,
            "full_name": profiles[r.user_id].full_name if r.user_id in profiles else None,
            "avatar_url": profiles[r.user_id].avatar_url if r.user_id in profiles else None,
            "text": r.text if r.deleted_at is None else "[message supprimé]",
            "reply_to_id": r.reply_to_id, "deleted_at": r.deleted_at, "created_at": r.created_at,
        }
        for r in rows
    ]
    return items, total


# ─── Moderation admin helpers ───────────────────────────────────────────────

def mute_user(db: Session, *, user_id: UUID, muted_by: Optional[UUID], duration_minutes: Optional[int], reason: Optional[str]) -> CompetitiveChatMute:
    from datetime import timedelta
    muted_until = _now() + timedelta(minutes=duration_minutes) if duration_minutes else None
    mute = CompetitiveChatMute(user_id=user_id, muted_by=muted_by, reason=reason, muted_until=muted_until)
    db.add(mute)
    db.commit()
    db.refresh(mute)
    return mute


def unmute_user(db: Session, *, user_id: UUID) -> int:
    """Ends every currently-active mute for this user (sets muted_until to
    now — history is preserved, never deleted)."""
    now = _now()
    rows = db.exec(select(CompetitiveChatMute).where(CompetitiveChatMute.user_id == user_id)).all()
    count = 0
    for row in rows:
        if row.muted_until is None or row.muted_until > now:
            row.muted_until = now
            db.add(row)
            count += 1
    if count:
        db.commit()
    return count


def list_active_mutes(db: Session) -> List[Dict]:
    now = _now()
    rows = db.exec(select(CompetitiveChatMute).order_by(CompetitiveChatMute.created_at.desc())).all()
    active = [r for r in rows if r.muted_until is None or r.muted_until > now]
    profiles = {p.id: p for p in db.exec(select(Profile).where(Profile.id.in_({r.user_id for r in active}))).all()} if active else {}
    return [
        {
            "id": r.id, "user_id": r.user_id,
            "full_name": profiles[r.user_id].full_name if r.user_id in profiles else None,
            "muted_by": r.muted_by, "reason": r.reason, "muted_until": r.muted_until, "created_at": r.created_at,
        }
        for r in active
    ]


def add_blocked_word(db: Session, *, word: str, created_by: Optional[UUID]) -> CompetitiveBlockedWord:
    existing = db.exec(select(CompetitiveBlockedWord).where(func.lower(CompetitiveBlockedWord.word) == word.lower())).first()
    if existing is not None:
        return existing
    row = CompetitiveBlockedWord(word=word.lower(), created_by=created_by)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_blocked_words(db: Session) -> List[CompetitiveBlockedWord]:
    return list(db.exec(select(CompetitiveBlockedWord).order_by(CompetitiveBlockedWord.created_at.desc())).all())


def delete_blocked_word(db: Session, *, word_id: UUID) -> None:
    row = db.get(CompetitiveBlockedWord, word_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mot introuvable.")
    db.delete(row)
    db.commit()


def list_chat_reports(db: Session, *, resolved: Optional[bool] = False, page: int = 1, size: int = 20) -> Tuple[List[Dict], int]:
    query = select(CompetitiveChatReport)
    if resolved is not None:
        query = query.where(CompetitiveChatReport.resolved_at.is_(None) if not resolved else CompetitiveChatReport.resolved_at.is_not(None))
    total = db.exec(select(func.count()).select_from(query.subquery())).one()
    rows = list(
        db.exec(query.order_by(CompetitiveChatReport.created_at.desc()).offset((page - 1) * size).limit(size)).all()
    )
    message_ids = {r.message_id for r in rows}
    reporter_ids = {r.reporter_id for r in rows}
    messages = {m.id: m for m in db.exec(select(CompetitiveMatchChat).where(CompetitiveMatchChat.id.in_(message_ids))).all()} if message_ids else {}
    profiles = {p.id: p for p in db.exec(select(Profile).where(Profile.id.in_(reporter_ids))).all()} if reporter_ids else {}
    items = [
        {
            "id": r.id, "message_id": r.message_id,
            "message_text": messages[r.message_id].text if r.message_id in messages else None,
            "reporter_id": r.reporter_id,
            "reporter_name": profiles[r.reporter_id].full_name if r.reporter_id in profiles else None,
            "reason": r.reason, "resolved_at": r.resolved_at, "resolved_by": r.resolved_by, "created_at": r.created_at,
        }
        for r in rows
    ]
    return items, total


def resolve_chat_report(db: Session, *, report_id: UUID, action: str) -> CompetitiveChatReport:
    report = db.get(CompetitiveChatReport, report_id)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Signalement introuvable.")
    if action == "delete_message":
        message = db.get(CompetitiveMatchChat, report.message_id)
        if message is not None and message.deleted_at is None:
            message.deleted_at = _now()
            db.add(message)
            publish_spectator_event(str(message.match_id), {"type": "chat_deleted", "message_id": str(message.id)})
            # Phase 13 — a deleted message means the report was CONFIRMED
            # (as opposed to dismissed, which leaves the message alone) —
            # only a confirmed report should cost the author Fair Play.
            from app.services.competitive import fair_play_service
            fair_play_service.penalize_confirmed_report(db, message.user_id, match_id=message.match_id)
    report.resolved_at = _now()
    db.add(report)
    db.commit()
    db.refresh(report)
    return report


# ─── Bookmarks ───────────────────────────────────────────────────────────────

def add_bookmark(db: Session, *, match_id: UUID, user_id: UUID) -> CompetitiveMatchBookmark:
    existing = db.exec(
        select(CompetitiveMatchBookmark)
        .where(CompetitiveMatchBookmark.match_id == match_id)
        .where(CompetitiveMatchBookmark.user_id == user_id)
    ).first()
    if existing is not None:
        return existing
    bookmark = CompetitiveMatchBookmark(match_id=match_id, user_id=user_id)
    db.add(bookmark)
    db.commit()
    db.refresh(bookmark)
    return bookmark


def remove_bookmark(db: Session, *, match_id: UUID, user_id: UUID) -> None:
    existing = db.exec(
        select(CompetitiveMatchBookmark)
        .where(CompetitiveMatchBookmark.match_id == match_id)
        .where(CompetitiveMatchBookmark.user_id == user_id)
    ).first()
    if existing is not None:
        db.delete(existing)
        db.commit()
