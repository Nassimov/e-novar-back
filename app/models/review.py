from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class Review(SQLModel, table=True):
    """
    Mirrors public.reviews.
    UNIQUE on (student_id, session_id) — one review per student per session.
    The recompute_teacher_rating() trigger in Supabase updates teacher_profiles.rating_avg.
    """

    __tablename__ = "reviews"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    student_id: UUID = Field(foreign_key="profiles.id", index=True)
    teacher_id: UUID = Field(foreign_key="profiles.id", index=True)
    session_id: Optional[UUID] = Field(default=None, foreign_key="sessions.id")
    rating: int = Field(ge=1, le=5)
    comment: Optional[str] = Field(default=None)
    status: str = Field(default="visible")               # public.review_status
    created_at: datetime = Field(default_factory=datetime.utcnow)
    moderated_by: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    moderated_at: Optional[datetime] = Field(default=None)

    __table_args__ = (
        sa.UniqueConstraint("student_id", "session_id", name="uq_review_student_session"),
    )


class Favorite(SQLModel, table=True):
    """
    Mirrors public.favorites.
    UNIQUE on (student_id, teacher_id) — a student can only favorite a teacher once.
    """

    __tablename__ = "favorites"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    student_id: UUID = Field(foreign_key="profiles.id", index=True)
    teacher_id: UUID = Field(foreign_key="profiles.id", index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    __table_args__ = (
        sa.UniqueConstraint("student_id", "teacher_id", name="uq_favorite"),
    )
