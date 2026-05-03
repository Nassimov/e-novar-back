from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class AiConversation(SQLModel, table=True):
    """
    Mirrors public.ai_conversations.
    A conversation thread between a user and the AI tutor.
    tokens_used: cumulative LLM tokens consumed (cost tracking).
    """

    __tablename__ = "ai_conversations"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    subject_id: Optional[UUID] = Field(default=None, foreign_key="subjects.id")
    started_at: datetime = Field(default_factory=datetime.utcnow)
    ended_at: Optional[datetime] = Field(default=None)
    tokens_used: int = Field(default=0)


class AiMessage(SQLModel, table=True):
    """
    Mirrors public.ai_messages.
    role: 'user' | 'assistant' | 'system'
    """

    __tablename__ = "ai_messages"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    conv_id: UUID = Field(foreign_key="ai_conversations.id", index=True)
    role: str = Field()                                  # user | assistant | system
    content: str = Field()
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AiProgress(SQLModel, table=True):
    """
    Mirrors public.ai_progress.
    UNIQUE on (student_id, subject_id) — one progress row per student per subject.
    mastery_pct: 0–100% computed mastery of the subject.
    weak_topics: jsonb array of topic slugs the student struggles with.
    """

    __tablename__ = "ai_progress"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    student_id: UUID = Field(foreign_key="profiles.id", index=True)
    subject_id: UUID = Field(foreign_key="subjects.id", index=True)
    mastery_pct: float = Field(default=0.0)
    weak_topics: Optional[Any] = Field(
        default=None,
        sa_column=sa.Column(JSONB, nullable=True, server_default="'[]'::jsonb"),
    )
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    __table_args__ = (
        sa.UniqueConstraint("student_id", "subject_id", name="uq_ai_progress"),
    )


class PracticeAttempt(SQLModel, table=True):
    """
    Mirrors public.practice_attempts.
    Records a student's AI-generated practice exercise attempt.
    score: 0–100 percentage.
    """

    __tablename__ = "practice_attempts"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    student_id: UUID = Field(foreign_key="profiles.id", index=True)
    subject_id: Optional[UUID] = Field(default=None, foreign_key="subjects.id")
    level_id: Optional[UUID] = Field(default=None, foreign_key="levels.id")
    score: Optional[float] = Field(default=None)
    duration_sec: Optional[int] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
