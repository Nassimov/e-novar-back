from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class SessionResponse(BaseModel):
    id: UUID
    booking_id: Optional[UUID] = None
    student_id: UUID
    teacher_id: UUID
    subject: Optional[str] = None
    level: Optional[str] = None
    scheduled_at: datetime
    duration_minutes: int
    mode: str
    status: str
    video_room_id: Optional[str] = None
    replay_url: Optional[str] = None
    rating: Optional[int] = None
    feedback: Optional[str] = None
    notes: Optional[str] = None
    ai_summary: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class SessionListResponse(BaseModel):
    items: list[SessionResponse]
    total: int
    page: int
    size: int
    pages: int


class SessionRateRequest(BaseModel):
    rating: int = Field(ge=1, le=5)
    feedback: Optional[str] = None


class JoinSessionResponse(BaseModel):
    session_id: UUID
    room_id: str
    join_url: str
    token: str
