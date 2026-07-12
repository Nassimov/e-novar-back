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
    type: str = Field(default="individual")              # public.session_type
    max_students: int = Field(default=1)
    mode: str = Field(default="online")                  # public.teaching_mode
    price: int = Field(default=0)                        # DZD — single session; pack5/pack10 are
                                                           # always computed on read via
                                                           # app.services.pricing, never stored.
    status: str = Field(default="open")                  # public.slot_status
    created_at: datetime = Field(default_factory=datetime.utcnow)


class TeacherSlotSubject(SQLModel, table=True):
    """
    Mirrors public.teacher_slot_subjects.
    A slot's subject/level combos — multiple per slot (e.g. a single slot can
    accept Physics:4AM, Physics:3AS and Maths:3AS at once). Replaces the old
    singular teacher_slots.subject_id/level_id columns (migration 050).
    """

    __tablename__ = "teacher_slot_subjects"
    __table_args__ = (
        sa.UniqueConstraint("slot_id", "subject_id", "level_id", name="uq_slot_subject_level"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    slot_id: UUID = Field(foreign_key="teacher_slots.id", index=True)
    subject_id: UUID = Field(foreign_key="subjects.id")
    level_id: UUID = Field(foreign_key="levels.id")
