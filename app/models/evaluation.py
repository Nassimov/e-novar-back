from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class Evaluation(SQLModel, table=True):
    """
    Mirrors public.evaluations.
    A teacher sends a formal evaluation to a student after a session.
    One evaluation per session (UNIQUE on session_id).
    skills: jsonb array of [{name, score}]
    """

    __tablename__ = "evaluations"
    __table_args__ = (
        sa.UniqueConstraint("session_id", name="uq_evaluation_session"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    session_id: UUID = Field(foreign_key="sessions.id", index=True)
    teacher_id: UUID = Field(foreign_key="profiles.id", index=True)
    student_id: UUID = Field(foreign_key="profiles.id", index=True)
    score_global: Optional[float] = Field(default=None)  # 0–20
    comment: Optional[str] = Field(default=None)
    skills: Optional[Any] = Field(
        default=None,
        sa_column=sa.Column(JSONB, nullable=True, server_default="'[]'::jsonb"),
    )
    kp_awarded: int = Field(default=0)
    status: str = Field(default="draft")                 # public.eval_status
    created_at: datetime = Field(default_factory=datetime.utcnow)
    sent_at: Optional[datetime] = Field(default=None)
