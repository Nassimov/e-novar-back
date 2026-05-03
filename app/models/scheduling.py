from __future__ import annotations

import datetime as dt
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class TeacherSlot(SQLModel, table=True):
    """
    Mirrors public.teacher_slots.
    Concrete date-specific availability slot (not a recurring pattern).
    status='open'    → bookable
    status='booked'  → already reserved
    status='blocked' → teacher manually blocked
    status='draft'   → not yet published
    """

    __tablename__ = "teacher_slots"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    teacher_id: UUID = Field(foreign_key="teacher_profiles.user_id", index=True)
    # Python attr 'slot_date' maps to DB column 'date' (avoids shadowing the dt.date type)
    slot_date: dt.date = Field(sa_column=sa.Column("date", sa.Date, nullable=False))
    start_time: dt.time = Field()
    end_time: dt.time = Field()
    subject_id: Optional[UUID] = Field(default=None, foreign_key="subjects.id")
    level_id: Optional[UUID] = Field(default=None, foreign_key="levels.id")
    type: str = Field(default="individual")              # public.session_type
    max_students: int = Field(default=1)
    mode: str = Field(default="online")                  # public.teaching_mode
    price: int = Field(default=0)                        # DZD
    status: str = Field(default="open")                  # public.slot_status
    created_at: datetime = Field(default_factory=datetime.utcnow)
