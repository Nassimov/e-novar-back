from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY
from sqlmodel import Field, SQLModel


class Chapter(SQLModel, table=True):
    __tablename__ = "chapters"

    id:         UUID     = Field(default_factory=uuid4, primary_key=True)
    subject_id: UUID     = Field(foreign_key="subjects.id", index=True)
    level_id:   Optional[UUID] = Field(default=None, foreign_key="levels.id")
    name:       str      = Field()
    slug:       Optional[str] = Field(default=None)
    position:   int      = Field(default=0)
    active:     bool     = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Question(SQLModel, table=True):
    __tablename__ = "questions"

    id:                 UUID     = Field(default_factory=uuid4, primary_key=True)
    subject_id:         UUID     = Field(foreign_key="subjects.id", index=True)
    school_level_id:    UUID     = Field(foreign_key="levels.id", index=True)
    chapter_id:         Optional[UUID] = Field(default=None, foreign_key="chapters.id")
    difficulty:         str      = Field()           # easy | medium | hard | expert
    type:               str      = Field(default="mcq")  # mcq | true_false | open
    statement:          str      = Field()
    explanation:        str      = Field(default="")
    source:             str      = Field(default="manual")  # manual | ai | imported
    validated:          bool     = Field(default=False)
    quality_score:      Optional[int] = Field(default=None)
    estimated_time_sec: int      = Field(default=30)
    tags:               Any      = Field(
        default=None,
        sa_column=sa.Column(ARRAY(sa.Text), nullable=True),
    )
    is_multi_choice:    bool     = Field(default=False)
    active:     bool     = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class QuestionChoice(SQLModel, table=True):
    __tablename__ = "question_choices"

    id:          UUID = Field(default_factory=uuid4, primary_key=True)
    question_id: UUID = Field(foreign_key="questions.id", index=True)
    label:       str  = Field()    # A | B | C | D | E
    text:        str  = Field()
    is_correct:  bool = Field(default=False)


class QuizAttempt(SQLModel, table=True):
    __tablename__ = "quiz_attempts"

    id:               UUID     = Field(default_factory=uuid4, primary_key=True)
    student_id:       UUID     = Field(foreign_key="profiles.id", index=True)
    subject_id:       Optional[UUID] = Field(default=None, foreign_key="subjects.id")
    school_level_id:  Optional[UUID] = Field(default=None, foreign_key="levels.id")
    difficulty:       str      = Field(default="medium")
    total_questions:  int      = Field(default=0)
    correct_answers:  int      = Field(default=0)
    score_percentage: Optional[float] = Field(default=None)
    ep_earned:        int      = Field(default=0)
    question_ids:     Any      = Field(
        default=None,
        sa_column=sa.Column(
            ARRAY(sa.dialects.postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default="{}",
        ),
    )
    started_at:   datetime          = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = Field(default=None)
    completed:    bool              = Field(default=False)
    # Anti-abuse audit trail
    ep_multiplier: float            = Field(default=1.0)
    fraud_flag:    bool             = Field(default=False)
    fraud_reason:  Optional[str]    = Field(default=None)


class QuizAnswer(SQLModel, table=True):
    __tablename__ = "quiz_answers"

    id:                  UUID = Field(default_factory=uuid4, primary_key=True)
    attempt_id:          UUID = Field(foreign_key="quiz_attempts.id", index=True)
    question_id:         UUID = Field(foreign_key="questions.id")
    selected_choice_ids: Any  = Field(
        default=None,
        sa_column=sa.Column(
            ARRAY(sa.dialects.postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default="{}",
        ),
    )
    is_correct:         bool         = Field(default=False)
    response_time_sec:  Optional[int] = Field(default=None)


class StudentSubjectMastery(SQLModel, table=True):
    __tablename__ = "student_subject_mastery"

    id:              UUID     = Field(default_factory=uuid4, primary_key=True)
    student_id:      UUID     = Field(foreign_key="profiles.id", index=True)
    subject_id:      UUID     = Field(foreign_key="subjects.id")
    school_level_id: Optional[UUID] = Field(default=None, foreign_key="levels.id")
    chapter_id:      Optional[UUID] = Field(default=None, foreign_key="chapters.id")
    mastery_pct:     float    = Field(default=0.0)
    attempts_count:  int      = Field(default=0)
    correct_count:   int      = Field(default=0)
    last_attempted:  Optional[datetime] = Field(default=None)
    updated_at:      datetime = Field(default_factory=datetime.utcnow)
