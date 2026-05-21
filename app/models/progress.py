from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class StudentMonthlySnapshot(SQLModel, table=True):
    """
    One row per student per calendar month.
    Persisted by GET /api/student/progress to enable month-over-month comparison.
    subject_mastery: [{"subject_id": str, "subject_name": str, "mastery_pct": float}]
    """

    __tablename__ = "student_monthly_snapshots"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    student_id: UUID = Field(foreign_key="profiles.id", index=True)
    snapshot_month: date = Field()          # always first day of month
    sessions_count: int = Field(default=0)
    homework_submitted_count: int = Field(default=0)
    homework_graded_count: int = Field(default=0)
    quiz_count: int = Field(default=0)
    ep_earned: int = Field(default=0)
    subject_mastery: Any = Field(
        default=None,
        sa_column=sa.Column(JSONB, nullable=False, server_default="'[]'::jsonb"),
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
