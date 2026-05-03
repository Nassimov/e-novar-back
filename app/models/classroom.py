from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class ClassroomRoom(SQLModel, table=True):
    """
    Mirrors public.classroom_rooms.
    LiveKit (or similar) room tied to a TutoringSession.
    UNIQUE on session_id — one room per session.
    """

    __tablename__ = "classroom_rooms"
    __table_args__ = (
        sa.UniqueConstraint("session_id", name="uq_classroom_room_session"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    session_id: UUID = Field(foreign_key="sessions.id", index=True)
    provider: str = Field(default="livekit")
    room_id: Optional[str] = Field(default=None)
    started_at: Optional[datetime] = Field(default=None)
    ended_at: Optional[datetime] = Field(default=None)
    recording_url: Optional[str] = Field(default=None)


class ClassroomMessage(SQLModel, table=True):
    """
    Mirrors public.classroom_messages.
    Chat messages sent during a live classroom session.
    type: 'text' | 'file' | 'system' | 'whiteboard'
    """

    __tablename__ = "classroom_messages"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    room_id: UUID = Field(foreign_key="classroom_rooms.id", index=True)
    sender_id: UUID = Field(foreign_key="profiles.id", index=True)
    body: Optional[str] = Field(default=None)
    type: str = Field(default="text")
    created_at: datetime = Field(default_factory=datetime.utcnow)
