from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlmodel import Field, SQLModel

# Re-export for backward compat
from app.models.enums import HomeworkStatus  # noqa: F401


class Homework(SQLModel, table=True):
    """
    Mirrors public.homeworks.
    hints: text[]              — ordered hint strings revealed progressively
    hints_checked: boolean[]   — tracks which hints the student has viewed
    attachments: jsonb[]       — [{name, url, type}]
    """

    __tablename__ = "homeworks"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    teacher_id: UUID = Field(foreign_key="profiles.id", index=True)
    student_id: UUID = Field(foreign_key="profiles.id", index=True)
    session_id: Optional[UUID] = Field(default=None, foreign_key="sessions.id")
    subject_id: Optional[UUID] = Field(default=None, foreign_key="subjects.id")
    title: str = Field()
    statement: str = Field()
    hints: Optional[List[str]] = Field(
        default=None,
        sa_column=sa.Column(ARRAY(sa.Text), nullable=True, server_default="{}"),
    )
    hints_checked: Optional[List[bool]] = Field(
        default=None,
        sa_column=sa.Column(ARRAY(sa.Boolean), nullable=True, server_default="{}"),
    )
    attachments: Optional[Any] = Field(
        default=None,
        sa_column=sa.Column(JSONB, nullable=True, server_default="'[]'::jsonb"),
    )
    due_at: Optional[datetime] = Field(default=None)
    due_label: Optional[str] = Field(default=None)
    status: str = Field(default="todo")                  # public.homework_status
    created_at: datetime = Field(default_factory=datetime.utcnow)


class HomeworkSubmission(SQLModel, table=True):
    """
    Mirrors public.homework_submissions.
    One submission per homework (UNIQUE on homework_id).
    files: jsonb array of [{name, url, type, size}]
    """

    __tablename__ = "homework_submissions"
    __table_args__ = (
        sa.UniqueConstraint("homework_id", name="uq_hw_submission"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    homework_id: UUID = Field(foreign_key="homeworks.id", index=True)
    student_id: UUID = Field(foreign_key="profiles.id", index=True)
    text: Optional[str] = Field(default=None)
    files: Optional[Any] = Field(
        default=None,
        sa_column=sa.Column(JSONB, nullable=True, server_default="'[]'::jsonb"),
    )
    submitted_at: datetime = Field(default_factory=datetime.utcnow)


class HomeworkGrade(SQLModel, table=True):
    """
    Mirrors public.homework_grades.
    One grade per homework (UNIQUE on homework_id).
    files: jsonb array of teacher's return documents.
    The handle_homework_grade() trigger in Supabase populates kp_awarded automatically.
    """

    __tablename__ = "homework_grades"
    __table_args__ = (
        sa.UniqueConstraint("homework_id", name="uq_hw_grade"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    homework_id: UUID = Field(foreign_key="homeworks.id", index=True)
    teacher_id: UUID = Field(foreign_key="profiles.id", index=True)
    score: float = Field(ge=0.0, le=20.0)
    feedback: Optional[str] = Field(default=None)
    files: Optional[Any] = Field(
        default=None,
        sa_column=sa.Column(JSONB, nullable=True, server_default="'[]'::jsonb"),
    )
    kp_awarded: int = Field(default=0)
    hints_used: int = Field(default=0)
    graded_at: datetime = Field(default_factory=datetime.utcnow)
