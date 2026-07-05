from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class Conversation(SQLModel, table=True):
    """
    Mirrors public.conversations.
    type: 'direct' | 'group' | 'support'
    """

    __tablename__ = "conversations"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    type: str = Field()
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ConversationParticipant(SQLModel, table=True):
    """
    Mirrors public.conversation_participants.
    Composite PK on (conv_id, user_id).
    role: 'owner' | 'member'
    hidden_at: set when the participant hides the conversation (NULL = visible).
               Messages are preserved; setting to NULL restores it.
    """

    __tablename__ = "conversation_participants"

    conv_id: UUID = Field(foreign_key="conversations.id", primary_key=True)
    user_id: UUID = Field(foreign_key="profiles.id", primary_key=True)
    role: Optional[str] = Field(default=None)
    last_read_at: Optional[datetime] = Field(default=None)
    hidden_at: Optional[datetime] = Field(default=None)


class ChatMessage(SQLModel, table=True):
    """
    Mirrors public.messages.
    attachments: jsonb array of [{name, url, type, size}]
    is_flagged / flag_reason: set by the suspicious-content scanner in send_message.
    Named ChatMessage to avoid conflicts with Python's built-in or other imports.
    """

    __tablename__ = "messages"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    conv_id: UUID = Field(foreign_key="conversations.id", index=True)
    sender_id: UUID = Field(foreign_key="profiles.id", index=True)
    body: Optional[str] = Field(default=None)
    attachments: Optional[Any] = Field(
        default=None,
        sa_column=sa.Column(JSONB, nullable=True, server_default="'[]'::jsonb"),
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)
    read_at: Optional[datetime] = Field(default=None)
    is_flagged: bool = Field(
        default=False,
        sa_column=sa.Column(sa.Boolean, nullable=False, server_default=sa.text("false")),
    )
    flag_reason: Optional[str] = Field(default=None)
