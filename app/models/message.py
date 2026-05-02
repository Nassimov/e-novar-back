from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from sqlmodel import Field, SQLModel


class Conversation(SQLModel, table=True):
    __tablename__ = "conversations"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    participant_ids: str = Field()  # JSON array of user UUIDs
    last_message_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Message(SQLModel, table=True):
    __tablename__ = "messages"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    conversation_id: UUID = Field(foreign_key="conversations.id", index=True)
    sender_id: UUID = Field(foreign_key="users.id", index=True)
    content: str = Field()
    is_read: bool = Field(default=False)
    attachment_url: Optional[str] = Field(default=None)
    attachment_type: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
