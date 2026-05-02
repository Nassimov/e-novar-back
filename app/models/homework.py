from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID, uuid4

from sqlmodel import Field, SQLModel


class HomeworkStatus(str, Enum):
    todo = "todo"
    submitted = "submitted"
    graded = "graded"


class Homework(SQLModel, table=True):
    __tablename__ = "homeworks"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    teacher_id: UUID = Field(foreign_key="users.id", index=True)
    student_id: UUID = Field(foreign_key="users.id", index=True)
    session_id: Optional[UUID] = Field(default=None, foreign_key="sessions.id", index=True)
    subject: str = Field()
    title: str = Field()
    statement: str = Field()
    hints: Optional[str] = Field(default="[]")  # JSON array of hint strings
    due_at: Optional[datetime] = Field(default=None)
    status: HomeworkStatus = Field(default=HomeworkStatus.todo)
    kp_reward: int = Field(default=50)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class HomeworkSubmission(SQLModel, table=True):
    __tablename__ = "homework_submissions"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    homework_id: UUID = Field(foreign_key="homeworks.id", unique=True, index=True)
    text: str = Field()
    attachment_url: Optional[str] = Field(default=None)
    submitted_at: datetime = Field(default_factory=datetime.utcnow)


class HomeworkGrade(SQLModel, table=True):
    __tablename__ = "homework_grades"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    homework_id: UUID = Field(foreign_key="homeworks.id", unique=True, index=True)
    score: float = Field(ge=0.0, le=20.0)
    feedback: Optional[str] = Field(default=None)
    kp_awarded: int = Field(default=0)
    graded_at: datetime = Field(default_factory=datetime.utcnow)
