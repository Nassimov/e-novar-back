from __future__ import annotations

"""Clash Club — Part B — Chat API. Mounted at prefix="/api" (see app/main.py),
full paths declared per route, same convention as clubs.py."""

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session

from app.core.club_ws import publish_club_event
from app.dependencies import get_current_user, get_db
from app.schemas.club import ChatMessageOut, SendChatMessageRequest
from app.services.club import chat_service
from app.services.club.permission_service import get_club_or_404, require_permission

router = APIRouter(tags=["Clubs"])


@router.post("/clubs/{club_id}/chat/messages", response_model=ChatMessageOut)
def send_message(
    club_id: UUID, payload: SendChatMessageRequest,
    current_user: Dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    club = get_club_or_404(db, club_id)
    message = chat_service.send_message(
        db, club, user_id=user_id, text=payload.text, reply_to_id=payload.reply_to_id,
        attachments=payload.attachments, is_announcement=payload.is_announcement,
    )
    publish_club_event(str(club_id), {"type": "chat_message", "message": {**message, "id": str(message["id"]), "club_id": str(club_id), "user_id": str(user_id), "created_at": message["created_at"].isoformat()}})
    return message


@router.get("/clubs/{club_id}/chat/messages", response_model=List[ChatMessageOut])
def list_messages(
    club_id: UUID, before: Optional[datetime] = Query(default=None), limit: int = Query(default=50, ge=1, le=100),
    current_user: Dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db),
):
    get_club_or_404(db, club_id)
    require_permission(db, club_id, UUID(current_user["id"]), "chat")
    return chat_service.list_messages(db, club_id, before=before, limit=limit)


@router.get("/clubs/{club_id}/chat/pinned", response_model=List[ChatMessageOut])
def list_pinned(
    club_id: UUID, current_user: Dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db),
):
    get_club_or_404(db, club_id)
    require_permission(db, club_id, UUID(current_user["id"]), "chat")
    return chat_service.list_pinned(db, club_id)


@router.post("/clubs/{club_id}/chat/messages/{message_id}/pin", response_model=ChatMessageOut)
def pin_message(
    club_id: UUID, message_id: UUID, current_user: Dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db),
):
    actor_id = UUID(current_user["id"])
    message = chat_service.pin_message(db, club_id, message_id, actor_id, pinned=True)
    publish_club_event(str(club_id), {"type": "chat_pinned", "message_id": str(message_id)})
    return message


@router.post("/clubs/{club_id}/chat/messages/{message_id}/unpin", response_model=ChatMessageOut)
def unpin_message(
    club_id: UUID, message_id: UUID, current_user: Dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db),
):
    actor_id = UUID(current_user["id"])
    message = chat_service.pin_message(db, club_id, message_id, actor_id, pinned=False)
    publish_club_event(str(club_id), {"type": "chat_unpinned", "message_id": str(message_id)})
    return message


@router.delete("/clubs/{club_id}/chat/messages/{message_id}", status_code=204)
def delete_message(
    club_id: UUID, message_id: UUID, current_user: Dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db),
):
    actor_id = UUID(current_user["id"])
    chat_service.delete_message(db, club_id, message_id, actor_id)
    publish_club_event(str(club_id), {"type": "chat_deleted", "message_id": str(message_id)})
