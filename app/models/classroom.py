from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class ClassroomRoom(SQLModel, table=True):
    """
    Mirrors public.classroom_rooms.
    LiveKit (or similar) room tied to a TutoringSession.
    UNIQUE on session_id — one room per session.
    """

    __tablename__ = "classroom_rooms"
    __table_args__ = (
        sa.UniqueConstraint("session_id", name="uq_classroom_room_session"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    session_id: UUID = Field(foreign_key="sessions.id", index=True)
    provider: str = Field(default="livekit")
    room_id: Optional[str] = Field(default=None)
    started_at: Optional[datetime] = Field(default=None)
    ended_at: Optional[datetime] = Field(default=None)
    recording_url: Optional[str] = Field(default=None)


class ClassroomMessage(SQLModel, table=True):
    """
    Mirrors public.classroom_messages.
    Chat messages sent during a live classroom session.
    type: 'text' | 'file' | 'system' | 'whiteboard'
    """

    __tablename__ = "classroom_messages"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    room_id: UUID = Field(foreign_key="classroom_rooms.id", index=True)
    sender_id: UUID = Field(foreign_key="profiles.id", index=True)
    body: Optional[str] = Field(default=None)
    type: str = Field(default="text")
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ─── Live classroom features (migration 066) ──────────────────────────────────
# The video call itself is a LiveKit room (see app/services/livekit_video.py)
# — its name is deterministic (f"session-{session_id}"), so unlike ClassroomRoom
# above there is nothing to persist for it. In-call chat reuses the real
# Conversation/ChatMessage system (app/models/conversation.py) instead of
# ClassroomMessage, so messages sent during a lesson stay in the student's/
# teacher's normal inbox afterward. Whiteboard strokes and screen-share
# annotations are relayed live over /ws/classroom/{session_id} only — not
# persisted, by design.

class SessionChapter(SQLModel, table=True):
    __tablename__ = "session_chapters"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    session_id: UUID = Field(foreign_key="sessions.id", index=True)
    title: str = Field()
    duration_min: int = Field(default=10)
    position: int = Field(default=0)
    status: str = Field(default="pending")  # 'pending' | 'active' | 'done'
    started_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class SessionFile(SQLModel, table=True):
    __tablename__ = "session_files"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    session_id: UUID = Field(foreign_key="sessions.id", index=True)
    uploaded_by: UUID = Field(foreign_key="profiles.id")
    name: str = Field()
    url: str = Field()
    mime: Optional[str] = Field(default=None)
    size_bytes: Optional[int] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class SessionQuiz(SQLModel, table=True):
    __tablename__ = "session_quizzes"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    session_id: UUID = Field(foreign_key="sessions.id", index=True)
    created_by: UUID = Field(foreign_key="profiles.id")
    question: str = Field()
    choices: List[str] = Field(default_factory=list, sa_column=sa.Column(JSONB, nullable=False))
    correct_index: int = Field()
    created_at: datetime = Field(default_factory=datetime.utcnow)


class SessionQuizAnswer(SQLModel, table=True):
    __tablename__ = "session_quiz_answers"
    __table_args__ = (
        sa.UniqueConstraint("quiz_id", "student_id", name="uq_session_quiz_answer"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    quiz_id: UUID = Field(foreign_key="session_quizzes.id", index=True)
    student_id: UUID = Field(foreign_key="profiles.id", index=True)
    choice_index: int = Field()
    is_correct: bool = Field()
    answered_at: datetime = Field(default_factory=datetime.utcnow)
