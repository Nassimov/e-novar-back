"""Admin — flagged message monitoring and moderation."""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, select

from app.dependencies import get_admin_user, get_db
from app.models.conversation import ChatMessage, ConversationParticipant
from app.models.profile import Profile

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Admin — Messages"])


# ── Schemas ────────────────────────────────────────────────────────────────────

class FlaggedMessageOut(BaseModel):
    id: str
    conv_id: str
    sender_id: str
    sender_name: str
    sender_avatar: Optional[str]
    recipient_id: str
    recipient_name: str
    recipient_avatar: Optional[str]
    body: Optional[str]
    flag_reason: str
    created_at: str


class FlaggedListOut(BaseModel):
    items: List[FlaggedMessageOut]
    total: int
    page: int
    pages: int


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_profile(uid: UUID, db: Session) -> Optional[Profile]:
    return db.exec(select(Profile).where(Profile.id == uid)).first()


def _other_participant(conv_id: UUID, sender_id: UUID, db: Session) -> Optional[UUID]:
    row = db.exec(
        select(ConversationParticipant).where(
            ConversationParticipant.conv_id == conv_id,
            ConversationParticipant.user_id != sender_id,
        )
    ).first()
    return row.user_id if row else None


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/flagged", response_model=FlaggedListOut)
async def list_flagged_messages(
    page: int = Query(1, ge=1),
    size: int = Query(25, ge=1, le=100),
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Paginated list of flagged messages, newest first."""
    all_flagged = db.exec(
        select(ChatMessage)
        .where(ChatMessage.is_flagged == True)  # noqa: E712
        .order_by(ChatMessage.created_at.desc())
    ).all()

    total = len(all_flagged)
    pages = max(1, math.ceil(total / size))
    page = min(page, pages)
    offset = (page - 1) * size
    page_items = all_flagged[offset : offset + size]

    items: List[FlaggedMessageOut] = []
    for msg in page_items:
        sender = _get_profile(msg.sender_id, db)
        recipient_id = _other_participant(msg.conv_id, msg.sender_id, db)
        recipient = _get_profile(recipient_id, db) if recipient_id else None

        items.append(FlaggedMessageOut(
            id=str(msg.id),
            conv_id=str(msg.conv_id),
            sender_id=str(msg.sender_id),
            sender_name=sender.full_name if sender else "Utilisateur inconnu",
            sender_avatar=sender.avatar_url if sender else None,
            recipient_id=str(recipient_id) if recipient_id else "",
            recipient_name=recipient.full_name if recipient else "Utilisateur inconnu",
            recipient_avatar=recipient.avatar_url if recipient else None,
            body=msg.body,
            flag_reason=msg.flag_reason or "inconnu",
            created_at=msg.created_at.isoformat(),
        ))

    return FlaggedListOut(items=items, total=total, page=page, pages=pages)


@router.post("/{message_id}/dismiss", status_code=200)
async def dismiss_flag(
    message_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Remove the flag from a message without any further action."""
    msg = db.exec(select(ChatMessage).where(ChatMessage.id == message_id)).first()
    if not msg:
        raise HTTPException(status_code=404, detail="Message introuvable.")
    if not msg.is_flagged:
        return {"ok": True, "already_dismissed": True}

    msg.is_flagged = False
    msg.flag_reason = None
    db.add(msg)
    db.commit()
    return {"ok": True}


@router.post("/{message_id}/warn", status_code=200)
async def warn_sender(
    message_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """
    Send an in-app warning notification to the message sender,
    then dismiss the flag. The message itself is preserved.
    """
    msg = db.exec(select(ChatMessage).where(ChatMessage.id == message_id)).first()
    if not msg:
        raise HTTPException(status_code=404, detail="Message introuvable.")

    # Dismiss the flag
    msg.is_flagged = False
    msg.flag_reason = None
    db.add(msg)
    db.commit()

    from app.services.notification_engine import emit
    emit(
        db, event_type="warning", user_id=msg.sender_id,
        title_override="Avertissement E-NOVAR",
        body_override=(
            "Un message que vous avez envoyé a été signalé car il suggère de "
            "poursuivre la collaboration hors de la plateforme. "
            "Rappel : toutes les interactions pédagogiques et financières doivent "
            "rester sur E-NOVAR pour votre sécurité et celle de l'autre partie."
        ),
    )
    return {"ok": True}
