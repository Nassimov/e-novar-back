from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class LeaderboardRankSnapshot(SQLModel, table=True):
    """
    Mirrors public.leaderboard_rank_snapshots.
    Caches a user's computed rank for a given period/audience/sort combination.
    Written by GET /api/student/leaderboard as a fire-and-forget side-effect.
    """

    __tablename__ = "leaderboard_rank_snapshots"
    __table_args__ = (
        sa.UniqueConstraint(
            "period_type", "period_key", "audience", "sort_by", "user_id",
            name="uq_lb_snap",
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    period_type: str = Field()       # 'weekly' | 'monthly' | 'global'
    period_key: str = Field()        # '2026-W21' | '2026-05' | 'all'
    audience: str = Field()          # 'students' | 'teachers'
    sort_by: str = Field()           # 'kp' | 'sessions' | 'rating'
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    rank: int = Field()
    score: float = Field()
    created_at: datetime = Field(default_factory=datetime.utcnow)


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
