from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel


class SessionResponse(BaseModel):
    id: UUID
    booking_id: Optional[UUID] = None
    teacher_id: UUID
    student_id: UUID
    subject_id: Optional[UUID] = None
    subject_name: Optional[str] = None
    level_id: Optional[UUID] = None
    level_name: Optional[str] = None
    scheduled_at: datetime
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    duration_min: Optional[int] = None
    mode: str
    status: str
    room_url: Optional[str] = None
    replay_url: Optional[str] = None
    summary: Optional[str] = None
    notes_teacher: Optional[str] = None
    teacher_payout_amount: int
    no_show: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class SessionListResponse(BaseModel):
    items: list[SessionResponse]
    total: int
    page: int
    size: int
    pages: int


class JoinSessionResponse(BaseModel):
    session_id: UUID
    room_id: str
    join_url: str
    token: str
