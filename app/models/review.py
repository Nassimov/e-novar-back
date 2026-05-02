from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from sqlmodel import Field, SQLModel


class Review(SQLModel, table=True):
    __tablename__ = "reviews"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    teacher_id: UUID = Field(foreign_key="users.id", index=True)
    student_id: UUID = Field(foreign_key="users.id", index=True)
    booking_id: Optional[UUID] = Field(default=None, foreign_key="bookings.id")
    rating: int = Field(ge=1, le=5)
    comment: Optional[str] = Field(default=None)
    is_flagged: bool = Field(default=False)
    flag_reason: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
