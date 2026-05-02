from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel


class MessageSend(BaseModel):
    conversation_id: Optional[UUID] = None
    recipient_id: Optional[UUID] = None  # used to create new conversation
    content: str
    attachment_url: Optional[str] = None
    attachment_type: Optional[str] = None


class MessageResponse(BaseModel):
    id: UUID
    conversation_id: UUID
    sender_id: UUID
    content: str
    is_read: bool
    attachment_url: Optional[str] = None
    attachment_type: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ConversationResponse(BaseModel):
    id: UUID
    participant_ids: List[str]
    last_message_at: Optional[datetime] = None
    last_message: Optional[str] = None
    unread_count: int = 0
    other_participant_name: Optional[str] = None
    other_participant_avatar: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}
