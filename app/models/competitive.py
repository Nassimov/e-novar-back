from __future__ import annotations

"""
Competitive Arena — Phase 1: Foundation & Architecture.

Mirrors migration 074_competitive_arena_foundation.sql. This phase only
builds the pre-match architecture (match lifecycle, lobby, invitations,
empty statistics row) — no gameplay/question-serving logic yet. See
app/services/competitive/ for the domain services and
app/routers/competitive/ for the REST API.

A "lobby" is not a separate table — a CompetitiveMatch IS the lobby before
it starts (status draft/waiting_for_opponent/accepted/scheduled/waiting_room/
countdown); this avoids duplicating a second near-identical entity.

Reused, not duplicated: profiles (creator/participants/invitees), subjects,
levels (existing reference tables), and the existing Question/QuestionChoice/
QuizAttempt/QuizAnswer tables in app/models/practice.py, which the future
Question Engine will read from.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY
from sqlmodel import Field, SQLModel

#: Every match type Phase 1's schema must support without future redesign.
MATCH_TYPES = ["duel", "club_battle", "battle_royale", "tournament"]

#: Full lifecycle. Phase 1 only ever drives a match up through
#: waiting_room/countdown (once every participant is ready) — in_progress
#: onward is reserved for the future gameplay engine (phase 2+).
MATCH_STATUSES = [
    "draft", "waiting_for_opponent", "accepted", "scheduled", "waiting_room",
    "countdown", "in_progress", "paused", "completed", "cancelled", "expired",
    "abandoned", "disconnected",
]

INVITATION_STATUSES = ["pending", "accepted", "declined", "cancelled", "expired"]

RANK_TIERS = ["bronze", "silver", "gold", "platinum", "diamond", "master", "legend"]


class CompetitiveMatch(SQLModel, table=True):
    """Mirrors public.competitive_matches. One row per match — also the lobby
    representation before the match starts (see module docstring)."""

    __tablename__ = "competitive_matches"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    match_type: str = Field()
    status: str = Field(default="draft")
    creator_id: UUID = Field(foreign_key="profiles.id", index=True)
    subject_ids: List[UUID] = Field(
        default_factory=list,
        sa_column=sa.Column(ARRAY(sa.UUID(as_uuid=True)), nullable=False, server_default="{}"),
    )
    school_level_id: Optional[UUID] = Field(default=None, foreign_key="levels.id")
    difficulty: Optional[str] = Field(default=None)
    question_count: int = Field(default=10)
    visibility: str = Field(default="private")
    max_players: int = Field(default=2)
    scheduled_at: Optional[datetime] = Field(default=None)
    started_at: Optional[datetime] = Field(default=None)
    completed_at: Optional[datetime] = Field(default=None)
    cancelled_at: Optional[datetime] = Field(default=None)
    cancelled_reason: Optional[str] = Field(default=None)
    expires_at: Optional[datetime] = Field(default=None)
    created_by: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    deleted_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CompetitiveMatchParticipant(SQLModel, table=True):
    """Mirrors public.competitive_match_participants."""

    __tablename__ = "competitive_match_participants"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    match_id: UUID = Field(foreign_key="competitive_matches.id", index=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    role: str = Field(default="player")  # player|spectator (future)
    is_ready: bool = Field(default=False)
    result: Optional[str] = Field(default=None)  # win|loss|draw — set by future gameplay engine
    score: int = Field(default=0)  # set by future gameplay engine
    joined_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    left_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CompetitiveInvitation(SQLModel, table=True):
    """Mirrors public.competitive_invitations. Tracked separately from
    participants since a declined/expired invite never becomes one."""

    __tablename__ = "competitive_invitations"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    match_id: UUID = Field(foreign_key="competitive_matches.id", index=True)
    inviter_id: UUID = Field(foreign_key="profiles.id", index=True)
    invitee_id: UUID = Field(foreign_key="profiles.id", index=True)
    status: str = Field(default="pending")
    expires_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc) + timedelta(minutes=15)
    )
    responded_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CompetitiveStatistics(SQLModel, table=True):
    """Mirrors public.competitive_statistics. One lazily-created row per
    student (see app/services/competitive/statistics_service.py). Phase 1
    only creates the schema — every counter stays at its zero default until
    a future phase wires real match results into it."""

    __tablename__ = "competitive_statistics"

    user_id: UUID = Field(primary_key=True, foreign_key="profiles.id")
    matches_played: int = Field(default=0)
    wins: int = Field(default=0)
    losses: int = Field(default=0)
    draws: int = Field(default=0)
    accuracy_pct: float = Field(default=0)
    avg_response_time_ms: Optional[int] = Field(default=None)
    current_streak: int = Field(default=0)
    longest_streak: int = Field(default=0)
    favorite_subject_id: Optional[UUID] = Field(default=None, foreign_key="subjects.id")
    favorite_difficulty: Optional[str] = Field(default=None)
    perfect_matches: int = Field(default=0)
    rank_tier: str = Field(default="bronze")
    best_rank_tier: str = Field(default="bronze")
    rating: int = Field(default=1000)
    tournament_wins: int = Field(default=0)
    club_wins: int = Field(default=0)
    battle_royale_wins: int = Field(default=0)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
