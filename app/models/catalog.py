from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class Subject(SQLModel, table=True):
    """Mirrors public.subjects — learning subjects catalogue."""

    __tablename__ = "subjects"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    slug: str = Field(sa_column=sa.Column(sa.String, unique=True, nullable=False))
    name: str = Field()
    icon: Optional[str] = Field(default=None)


class Level(SQLModel, table=True):
    """Mirrors public.levels — education levels catalogue."""

    __tablename__ = "levels"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    code: str = Field(sa_column=sa.Column(sa.String, unique=True, nullable=False))
    label: str = Field()
    position: int = Field(default=0)


class Wilaya(SQLModel, table=True):
    """Mirrors public.wilayas — Algerian provinces catalogue."""

    __tablename__ = "wilayas"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    code: Optional[str] = Field(
        default=None,
        sa_column=sa.Column(sa.String, unique=True, nullable=True),
    )
    name: str = Field()


# ── Teacher offerings ─────────────────────────────────────────────────────────

class TeacherSubjectPrice(SQLModel, table=True):
    """
    Mirrors public.teacher_subject_prices.
    Single source of truth for teacher offerings:
      one row = teacher can teach (subject) to (level) at (prices).

    Replaces the old teacher_subjects + teacher_levels split which could not
    express "teacher teaches Physics specifically to 1AS and 3AS".

    Useful queries:
      - Subjects a teacher offers:       SELECT DISTINCT subject_id WHERE teacher_id = X
      - Levels for a given subject:      SELECT level_id WHERE teacher_id = X AND subject_id = Y
      - Full catalogue with names:       JOIN subjects s, levels l WHERE teacher_id = X
    """

    __tablename__ = "teacher_subject_prices"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    teacher_id: UUID = Field(foreign_key="teacher_profiles.user_id", index=True)
    subject_id: UUID = Field(foreign_key="subjects.id", index=True)
    level_id: UUID = Field(foreign_key="levels.id", index=True)
    price_single: int = Field(default=0)
    price_pack5: int = Field(default=0)
    price_monthly: int = Field(default=0)
    lesson_format: str = Field(default="both")  # "individual" | "group" | "both"
    active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    __table_args__ = (
        sa.UniqueConstraint("teacher_id", "subject_id", "level_id", name="uq_teacher_subject_level"),
    )


class TeacherMode(SQLModel, table=True):
    """Mirrors public.teacher_modes — teaching modes offered by a teacher."""

    __tablename__ = "teacher_modes"

    teacher_id: UUID = Field(
        foreign_key="teacher_profiles.user_id",
        primary_key=True,
    )
    mode: str = Field(primary_key=True)               # public.teaching_mode value


class TeacherSessionType(SQLModel, table=True):
    """Mirrors public.teacher_session_types — individual/group offering."""

    __tablename__ = "teacher_session_types"

    teacher_id: UUID = Field(
        foreign_key="teacher_profiles.user_id",
        primary_key=True,
    )
    type: str = Field(primary_key=True)               # public.session_type value


class TeacherDiploma(SQLModel, table=True):
    """Mirrors public.teacher_diplomas — uploaded diploma documents."""

    __tablename__ = "teacher_diplomas"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    teacher_id: UUID = Field(foreign_key="teacher_profiles.user_id", index=True)
    name: str = Field()
    file_url: Optional[str] = Field(default=None)
    file_type: Optional[str] = Field(default=None)
    verified: bool = Field(default=False)
    uploaded_at: datetime = Field(default_factory=datetime.utcnow)
