from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from sqlmodel import Session, select

from app.core.redis import get_redis_client
from app.dependencies import get_current_user, get_db
from app.models.message import Conversation, Message
from app.models.user import User
from app.schemas.message import ConversationResponse, MessageResponse, MessageSend

router = APIRouter(tags=["messages"])


@router.get("/", response_model=List[ConversationResponse])
async def list_conversations(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List all conversations for the current user."""
    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    user_id_str = str(user.id)
    conversations = db.exec(select(Conversation)).all()
    user_conversations = [
        c for c in conversations
        if user_id_str in json.loads(c.participant_ids or "[]")
    ]

    result = []
    for conv in user_conversations:
        participant_ids = json.loads(conv.participant_ids or "[]")
        other_ids = [pid for pid in participant_ids if pid != user_id_str]

        other_name = None
        other_avatar = None
        if other_ids:
            try:
                other_uid = UUID(other_ids[0])
                other_user = db.get(User, other_uid)
                if other_user:
                    other_name = other_user.full_name
                    other_avatar = other_user.avatar_url
            except Exception:
                pass

        # Get last message
        last_msg = db.exec(
            select(Message)
            .where(Message.conversation_id == conv.id)
            .order_by(Message.created_at.desc())
        ).first()

        unread = db.exec(
            select(Message).where(
                Message.conversation_id == conv.id,
                Message.sender_id != user.id,
                Message.is_read == False,
            )
        ).all()

        result.append(ConversationResponse(
            id=conv.id,
            participant_ids=participant_ids,
            last_message_at=conv.last_message_at,
            last_message=last_msg.content if last_msg else None,
            unread_count=len(unread),
            other_participant_name=other_name,
            other_participant_avatar=other_avatar,
            created_at=conv.created_at,
        ))

    return result


@router.get("/{conversation_id}", response_model=List[MessageResponse])
async def get_messages(
    conversation_id: UUID,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=100),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get messages in a conversation."""
    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    conv = db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    participant_ids = json.loads(conv.participant_ids or "[]")
    if str(user.id) not in participant_ids:
        raise HTTPException(status_code=403, detail="Access denied")

    messages = db.exec(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc())
    ).all()

    total = len(messages)
    offset = (page - 1) * size
    paginated = list(reversed(messages[offset: offset + size]))

    return [MessageResponse.model_validate(m) for m in paginated]


@router.post("/", response_model=MessageResponse, status_code=status.HTTP_201_CREATED)
async def send_message(
    payload: MessageSend,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Send a message (create conversation if needed)."""
    from datetime import datetime

    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    conv_id = payload.conversation_id

    if conv_id is None:
        # Create new conversation
        if payload.recipient_id is None:
            raise HTTPException(status_code=400, detail="conversation_id or recipient_id required")

        recipient = db.get(User, payload.recipient_id)
        if recipient is None:
            raise HTTPException(status_code=404, detail="Recipient not found")

        # Check for existing conversation
        all_convs = db.exec(select(Conversation)).all()
        existing = None
        for c in all_convs:
            pids = set(json.loads(c.participant_ids or "[]"))
            if pids == {str(user.id), str(recipient.id)}:
                existing = c
                break

        if existing:
            conv_id = existing.id
        else:
            new_conv = Conversation(
                participant_ids=json.dumps([str(user.id), str(recipient.id)]),
                last_message_at=datetime.utcnow(),
            )
            db.add(new_conv)
            db.commit()
            db.refresh(new_conv)
            conv_id = new_conv.id

    conv = db.get(Conversation, conv_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    participant_ids = json.loads(conv.participant_ids or "[]")
    if str(user.id) not in participant_ids:
        raise HTTPException(status_code=403, detail="Access denied")

    message = Message(
        conversation_id=conv_id,
        sender_id=user.id,
        content=payload.content,
        attachment_url=payload.attachment_url,
        attachment_type=payload.attachment_type,
    )
    db.add(message)

    conv.last_message_at = datetime.utcnow()
    db.add(conv)
    db.commit()
    db.refresh(message)

    # Publish to Redis pub/sub for WebSocket delivery
    try:
        redis = get_redis_client()
        msg_data = json.dumps({
            "type": "new_message",
            "conversation_id": str(conv_id),
            "message": {
                "id": str(message.id),
                "sender_id": str(message.sender_id),
                "content": message.content,
                "created_at": message.created_at.isoformat(),
            },
        })
        for pid in participant_ids:
            redis.publish(f"user:{pid}:messages", msg_data)
    except Exception:
        pass

    return MessageResponse.model_validate(message)


@router.patch("/{message_id}/read")
async def mark_message_read(
    message_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mark a message as read."""
    message = db.get(Message, message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="Message not found")

    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()

    # Verify user is a participant
    conv = db.get(Conversation, message.conversation_id)
    if conv:
        participant_ids = json.loads(conv.participant_ids or "[]")
        if user and str(user.id) not in participant_ids:
            raise HTTPException(status_code=403, detail="Access denied")

    message.is_read = True
    db.add(message)
    db.commit()
    return {"message": "Marked as read"}
