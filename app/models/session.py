from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID, uuid4

from sqlmodel import Field, SQLModel


class SessionStatus(str, Enum):
    scheduled = "scheduled"
    live = "live"
    completed = "completed"
    cancelled = "cancelled"


class Session(SQLModel, table=True):
    __tablename__ = "sessions"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    booking_id: Optional[UUID] = Field(default=None, foreign_key="bookings.id", index=True)
    student_id: UUID = Field(foreign_key="users.id", index=True)
    teacher_id: UUID = Field(foreign_key="users.id", index=True)
    subject: Optional[str] = Field(default=None)
    level: Optional[str] = Field(default=None)
    scheduled_at: datetime = Field(default_factory=datetime.utcnow)
    duration_minutes: int = Field(default=60)
    mode: str = Field(default="online")
    status: SessionStatus = Field(default=SessionStatus.scheduled)
    video_room_id: Optional[str] = Field(default=None)
    replay_url: Optional[str] = Field(default=None)
    rating: Optional[int] = Field(default=None)
    feedback: Optional[str] = Field(default=None)
    notes: Optional[str] = Field(default=None)
    ai_summary: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
