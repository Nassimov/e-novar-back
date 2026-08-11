from __future__ import annotations

"""
Club Chat Service — Competitive Arena Phase 11, Part B.

Dedicated club-room chat (see app/models/club.py's ClubChatMessage docstring
for why this is its own table). Moderation reuses the EXISTING
CompetitiveBlockedWord substring filter verbatim (already generic, see
app/services/competitive/spectator_service.py's _contains_blocked_word) —
no new word list. Rate limiting mirrors spectator_service._rate_limit
(fail-open — a Redis hiccup must never block chat entirely).

Realtime delivery: app/main.py's /ws/club/{club_id} handler calls
publish_club_event() (app/core/club_ws.py) after send_message/pin/delete —
kept out of this module so chat_service stays testable without a live
Redis connection, same separation every other *_service.py in this codebase
already uses (WS publish lives in the router/main.py layer, not the
service)."""

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.models.club import Club, ClubChatMessage
from app.models.competitive import CompetitiveBlockedWord
from app.models.profile import Profile
from app.services.club.permission_service import get_club_or_404, require_permission
from app.services.notification_engine import emit

logger = logging.getLogger(__name__)

_MENTION_RE = re.compile(r"@\[([0-9a-fA-F-]{36})\]")  # frontend renders @[uuid] as a resolved @Name chip


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _rate_limit(*, key: str, limit: int, window_seconds: int = 10) -> None:
    from app.core.rate_limiter import check_rate_limit
    check_rate_limit(key=key, limit=limit, window_seconds=window_seconds)


def _contains_blocked_word(db: Session, text: str) -> bool:
    words = db.exec(select(CompetitiveBlockedWord.word)).all()
    lowered = text.lower()
    return any(w.lower() in lowered for w in words)


def _extract_mentions(text: str) -> List[UUID]:
    out = []
    for raw in _MENTION_RE.findall(text):
        try:
            out.append(UUID(raw))
        except ValueError:
            continue
    return out


def _message_to_out(db: Session, m: ClubChatMessage, *, profiles: Optional[Dict[UUID, Profile]] = None) -> Dict[str, Any]:
    if profiles is None:
        profiles = {}
        author = db.get(Profile, m.user_id)
        if author:
            profiles[m.user_id] = author
    author = profiles.get(m.user_id)
    return {
        "id": m.id, "club_id": m.club_id, "user_id": m.user_id,
        "author_name": author.full_name if author else None, "author_avatar_url": author.avatar_url if author else None,
        "text": "[Message supprimé]" if m.deleted_at else m.text,
        "mentions": m.mentions, "attachments": m.attachments, "reply_to_id": m.reply_to_id,
        "is_announcement": m.is_announcement, "is_pinned": m.is_pinned,
        "deleted": m.deleted_at is not None, "created_at": m.created_at,
    }


def send_message(
    db: Session, club: Club, *, user_id: UUID, text: str, reply_to_id: Optional[UUID] = None,
    attachments: Optional[List[Dict[str, Any]]] = None, is_announcement: bool = False,
) -> Dict[str, Any]:
    if is_announcement:
        require_permission(db, club.id, user_id, "post_announcement")
    else:
        require_permission(db, club.id, user_id, "chat")

    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Le message ne peut pas être vide.")
    if len(text) > 2000:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Message trop long (2000 caractères max).")
    if _contains_blocked_word(db, text):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Message refusé : contient un mot interdit.")

    _rate_limit(key=f"club:chat_rate:{club.id}:{user_id}", limit=10)

    if reply_to_id is not None:
        parent = db.get(ClubChatMessage, reply_to_id)
        if parent is None or parent.club_id != club.id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Message parent introuvable.")

    mentions = _extract_mentions(text)
    message = ClubChatMessage(
        club_id=club.id, user_id=user_id, text=text, mentions=mentions, attachments=attachments or [],
        reply_to_id=reply_to_id, is_announcement=is_announcement,
    )
    db.add(message)
    club.last_activity_at = _now()
    db.add(club)
    db.commit()
    db.refresh(message)

    if mentions:
        for mentioned_id in mentions:
            if mentioned_id == user_id:
                continue
            emit(
                db, event_type="club_message_mention", user_id=mentioned_id, context={"club_name": club.name},
                data={"club_id": str(club.id), "message_id": str(message.id)},
                dedup_key=f"club_message_mention:{message.id}:{mentioned_id}",
            )

    if is_announcement:
        from app.models.club import ClubMember
        members = db.exec(select(ClubMember).where(ClubMember.club_id == club.id).where(ClubMember.status == "active")).all()
        for m in members:
            if m.user_id == user_id:
                continue
            emit(
                db, event_type="club_announcement_posted", user_id=m.user_id, context={"club_name": club.name},
                data={"club_id": str(club.id), "message_id": str(message.id)},
                dedup_key=f"club_announcement_posted:{message.id}:{m.user_id}",
            )

    return _message_to_out(db, message)


def list_messages(db: Session, club_id: UUID, *, before: Optional[datetime] = None, limit: int = 50) -> List[Dict[str, Any]]:
    query = select(ClubChatMessage).where(ClubChatMessage.club_id == club_id)
    if before is not None:
        query = query.where(ClubChatMessage.created_at < before)
    rows = list(db.exec(query.order_by(ClubChatMessage.created_at.desc()).limit(limit)).all())
    profiles = {p.id: p for p in db.exec(select(Profile).where(Profile.id.in_([r.user_id for r in rows]))).all()} if rows else {}
    return [_message_to_out(db, r, profiles=profiles) for r in reversed(rows)]


def list_pinned(db: Session, club_id: UUID) -> List[Dict[str, Any]]:
    rows = list(db.exec(
        select(ClubChatMessage).where(ClubChatMessage.club_id == club_id)
        .where(ClubChatMessage.is_pinned == True).where(ClubChatMessage.deleted_at.is_(None))  # noqa: E712
        .order_by(ClubChatMessage.pinned_at.desc())
    ).all())
    profiles = {p.id: p for p in db.exec(select(Profile).where(Profile.id.in_([r.user_id for r in rows]))).all()} if rows else {}
    return [_message_to_out(db, r, profiles=profiles) for r in rows]


def pin_message(db: Session, club_id: UUID, message_id: UUID, actor_id: UUID, *, pinned: bool) -> Dict[str, Any]:
    require_permission(db, club_id, actor_id, "moderate_chat")
    message = db.get(ClubChatMessage, message_id)
    if message is None or message.club_id != club_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message introuvable.")
    message.is_pinned = pinned
    message.pinned_at = _now() if pinned else None
    message.pinned_by = actor_id if pinned else None
    db.add(message)
    db.commit()
    db.refresh(message)
    return _message_to_out(db, message)


def delete_message(db: Session, club_id: UUID, message_id: UUID, actor_id: UUID) -> None:
    message = db.get(ClubChatMessage, message_id)
    if message is None or message.club_id != club_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message introuvable.")
    if message.user_id != actor_id:
        require_permission(db, club_id, actor_id, "remove_messages")
    if message.deleted_at is not None:
        return
    message.deleted_at = _now()
    message.deleted_by = actor_id
    db.add(message)
    db.commit()
