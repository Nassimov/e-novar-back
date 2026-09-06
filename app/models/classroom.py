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
    correct_index: int = Field()  # deprecated — kept for old rows, see correct_indices
    correct_indices: List[int] = Field(default_factory=list, sa_column=sa.Column(JSONB, nullable=False))
    created_at: datetime = Field(default_factory=datetime.utcnow)


class SessionQuizAnswer(SQLModel, table=True):
    __tablename__ = "session_quiz_answers"
    __table_args__ = (
        sa.UniqueConstraint("quiz_id", "student_id", name="uq_session_quiz_answer"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    quiz_id: UUID = Field(foreign_key="session_quizzes.id", index=True)
    student_id: UUID = Field(foreign_key="profiles.id", index=True)
    choice_index: int = Field()  # deprecated — kept for old rows, see choice_indices
    choice_indices: List[int] = Field(default_factory=list, sa_column=sa.Column(JSONB, nullable=False))
    is_correct: bool = Field()
    answered_at: datetime = Field(default_factory=datetime.utcnow)


class SessionRecording(SQLModel, table=True):
    """Mirrors public.session_recordings — one row per LiveKit Egress
    recording attempt. `file_url`/`duration_sec` are only known once the
    upload finishes; no LiveKit webhook receiver exists in this app, so
    they're refreshed by polling ListEgress on read (see
    app/services/egress.py), not pushed. See app/routers/classroom.py's
    recording endpoints and migration 103."""

    __tablename__ = "session_recordings"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    session_id: UUID = Field(foreign_key="sessions.id", index=True)
    room_key: str = Field()
    egress_id: str = Field(unique=True, index=True)
    status: str = Field(default="active")  # 'active' | 'ending' | 'complete' | 'failed'
    file_url: Optional[str] = Field(default=None)
    duration_sec: Optional[int] = Field(default=None)
    started_by: UUID = Field(foreign_key="profiles.id")
    started_at: datetime = Field(default_factory=datetime.utcnow)
    ended_at: Optional[datetime] = Field(default=None)


class SessionNotepad(SQLModel, table=True):
    """Mirrors public.session_notepads. A free-form shared scratch space —
    distinct from the structured "chapters" program — either party can type,
    both see it live (broadcast over the classroom WS) and it survives a
    reload/reconnect (unlike whiteboard strokes, which are DB-less). One row
    per room_key (shared across a group lesson's siblings). See migration 104."""

    __tablename__ = "session_notepads"

    room_key: str = Field(primary_key=True)
    content: str = Field(default="")
    updated_by: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class SessionWhiteboardState(SQLModel, table=True):
    """Mirrors public.session_whiteboard_state. The currently shared
    background image (an uploaded exercise sheet/diagram) drawn behind the
    whiteboard canvas before strokes replay on top. One row per room_key.
    See migration 104."""

    __tablename__ = "session_whiteboard_state"

    room_key: str = Field(primary_key=True)
    background_url: Optional[str] = Field(default=None)
    background_file_id: Optional[UUID] = Field(default=None)
    updated_by: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class SessionBookmark(SQLModel, table=True):
    """Mirrors public.session_bookmarks. Either party can mark a moment
    mid-session — shown on the post-session summary page, linking to
    recording_url#t=<elapsed_sec> once a completed recording exists (see
    SessionRecording). See migration 105."""

    __tablename__ = "session_bookmarks"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    session_id: UUID = Field(foreign_key="sessions.id", index=True)
    room_key: str = Field()
    created_by: UUID = Field(foreign_key="profiles.id")
    label: Optional[str] = Field(default=None)
    elapsed_sec: int = Field()
    created_at: datetime = Field(default_factory=datetime.utcnow)


class SessionWhiteboardSnapshot(SQLModel, table=True):
    """Mirrors public.session_whiteboard_snapshots. A PNG capture of the
    whiteboard (background + strokes) either party exported mid/post-session
    — shown on the summary page next to recordings/bookmarks. See migration
    106. Formula overlays (KaTeX HTML, not canvas-drawn) are not part of the
    raster — same rendering-fragility tradeoff documented on the formula
    overlay itself."""

    __tablename__ = "session_whiteboard_snapshots"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    session_id: UUID = Field(foreign_key="sessions.id", index=True)
    room_key: str = Field()
    created_by: UUID = Field(foreign_key="profiles.id")
    image_url: str = Field()
    created_at: datetime = Field(default_factory=datetime.utcnow)
