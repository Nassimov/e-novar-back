from __future__ import annotations

import datetime as dt
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlmodel import Field, SQLModel

# Re-exports for backward compat
from app.models.enums import (  # noqa: F401
    BookingFormula,
    BookingStatus,
    SessionStatus,
    SessionType,
    TeachingMode,
)


class Booking(SQLModel, table=True):
    """
    Mirrors public.bookings.
    payment_id is a circular FK to payments.id; the DB constraint lives in Supabase SQL only.
    We store it as a plain UUID here to avoid SQLAlchemy circular-dependency issues.
    """

    __tablename__ = "bookings"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    student_id: UUID = Field(foreign_key="profiles.id", index=True)
    teacher_id: UUID = Field(foreign_key="profiles.id", index=True)
    slot_id: Optional[UUID] = Field(default=None, foreign_key="teacher_slots.id")
    formula: str = Field(default="single")               # public.booking_formula
    mode: str = Field(default="online")                  # public.teaching_mode
    session_type: str = Field(default="individual")      # public.session_type
    # Python attr 'booking_date' maps to DB column 'date' (avoids shadowing the dt.date type)
    booking_date: dt.date = Field(sa_column=sa.Column("date", sa.Date, nullable=False))
    slot_time: Optional[dt.time] = Field(default=None)
    duration_min: int = Field(default=90)
    amount: int = Field(default=0)                       # DZD
    currency: str = Field(default="DZD")
    kp_reward: int = Field(default=0)
    status: str = Field(default="pending")               # public.booking_status
    # Circular FK to payments.id — stored as plain UUID; constraint is in Supabase DB.
    payment_id: Optional[UUID] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class TutoringSession(SQLModel, table=True):
    """
    Mirrors public.sessions.
    Named TutoringSession to avoid collision with sqlmodel.Session.
    """

    __tablename__ = "sessions"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    booking_id: Optional[UUID] = Field(default=None, foreign_key="bookings.id", index=True)
    teacher_id: UUID = Field(foreign_key="profiles.id", index=True)
    student_id: UUID = Field(foreign_key="profiles.id", index=True)
    subject_id: Optional[UUID] = Field(default=None, foreign_key="subjects.id")
    level_id: Optional[UUID] = Field(default=None, foreign_key="levels.id")
    scheduled_at: datetime = Field()
    started_at: Optional[datetime] = Field(default=None)
    ended_at: Optional[datetime] = Field(default=None)
    duration_min: Optional[int] = Field(default=None)
    mode: str = Field(default="online")                  # public.teaching_mode
    status: str = Field(default="scheduled")             # public.session_status
    room_url: Optional[str] = Field(default=None)
    summary: Optional[str] = Field(default=None)
    notes_teacher: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class SessionAttendee(SQLModel, table=True):
    """
    Mirrors public.session_attendees.
    Tracks all students who joined a group session.
    """

    __tablename__ = "session_attendees"
    __table_args__ = (
        sa.UniqueConstraint("session_id", "student_id", name="uq_session_attendee"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    session_id: UUID = Field(foreign_key="sessions.id", index=True)
    student_id: UUID = Field(foreign_key="profiles.id", index=True)
    joined_at: Optional[datetime] = Field(default=None)
    left_at: Optional[datetime] = Field(default=None)
