from __future__ import annotations

"""
LiveOps models — Competitive Arena Phase 15 (migration 095).

See the migration's own header for the full architecture rationale
(discriminator columns over parallel tables, no stored calendar table,
arena_missions reused verbatim as event objectives via period='event').
"""

from datetime import date, datetime
from typing import Any, Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

MISSION_TYPES = ["counter", "boolean", "accumulation", "streak", "time_based"]
MISSION_PERIODS = ["daily", "weekly", "monthly", "seasonal", "event"]
MISSION_DIFFICULTIES = ["easy", "normal", "hard", "expert", "legendary"]
MISSION_STATUSES = ["draft", "published", "paused", "archived"]
PLAYER_MISSION_STATUSES = ["active", "completed", "claimed", "expired"]

EVENT_TYPES = ["limited_time", "community_challenge", "happy_hour", "double_rewards", "educational_campaign"]
EVENT_STATUSES = ["draft", "scheduled", "active", "ended", "cancelled", "archived"]
EVENT_VISIBILITIES = ["public", "targeted"]


class ArenaEvent(SQLModel, table=True):
    """Mirrors public.arena_events — one table for every LiveOps event kind
    (Limited-Time Events, Happy Hour, Double Rewards, Community Challenges,
    Educational Campaigns), discriminated by `event_type`. `config`'s shape
    depends on event_type: happy_hour/double_rewards -> {multiplier,
    applies_to: [reward_type,...]}; community_challenge -> {metric_key,
    target_value}. An event's objectives are ordinary ArenaMission rows
    with period='event' and event_id=this row's id."""

    __tablename__ = "arena_events"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    event_type: str = Field()
    code: str = Field()
    name: str = Field()
    banner_url: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None)
    rules: Optional[str] = Field(default=None)
    starts_at: datetime = Field()
    ends_at: datetime = Field()
    status: str = Field(default="draft")
    visibility: str = Field(default="public")
    eligible_filter: Any = Field(default_factory=dict, sa_column=sa.Column(JSONB, nullable=False, server_default="'{}'::jsonb"))
    config: Any = Field(default_factory=dict, sa_column=sa.Column(JSONB, nullable=False, server_default="'{}'::jsonb"))
    reward_config: Any = Field(default_factory=list, sa_column=sa.Column(JSONB, nullable=False, server_default="'[]'::jsonb"))
    created_by: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ArenaMission(SQLModel, table=True):
    """Mirrors public.arena_missions — the admin-managed catalogue/pool.
    period + mission_type + metric_key is the full dispatcher — see
    app/services/competitive/mission_service.py."""

    __tablename__ = "arena_missions"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    code: str = Field()
    title: str = Field()
    description: Optional[str] = Field(default=None)
    icon: Optional[str] = Field(default=None)
    category: str = Field(default="Arena")
    mission_type: str = Field()
    period: str = Field()
    difficulty: str = Field(default="normal")
    metric_key: str = Field()
    target_value: int = Field(default=1)
    time_window: Any = Field(default_factory=dict, sa_column=sa.Column(JSONB, nullable=False, server_default="'{}'::jsonb"))
    reward_config: Any = Field(default_factory=list, sa_column=sa.Column(JSONB, nullable=False, server_default="'[]'::jsonb"))
    season_id: Optional[UUID] = Field(default=None, foreign_key="competitive_seasons.id")
    event_id: Optional[UUID] = Field(default=None, foreign_key="arena_events.id")
    max_free_rerolls: Optional[int] = Field(default=None)
    sort_order: int = Field(default=0)
    status: str = Field(default="draft")
    created_by: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ArenaPlayerMission(SQLModel, table=True):
    """Mirrors public.arena_player_missions — the per-player assigned
    instance for a given period_key. target_value is snapshotted at
    assignment time. UNIQUE(user_id, mission_id, period_key)."""

    __tablename__ = "arena_player_missions"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    mission_id: UUID = Field(foreign_key="arena_missions.id", index=True)
    period_key: str = Field()
    target_value: int = Field()
    progress_current: int = Field(default=0)
    status: str = Field(default="active")
    assigned_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = Field(default=None)
    claimed_at: Optional[datetime] = Field(default=None)
    expires_at: Optional[datetime] = Field(default=None)
    rerolled_count: int = Field(default=0)

    __table_args__ = (
        sa.UniqueConstraint("user_id", "mission_id", "period_key", name="uq_arena_player_missions"),
    )


class ArenaLoginReward(SQLModel, table=True):
    """Mirrors public.arena_login_rewards — the N-day login calendar
    catalogue (admin-configurable length via PlatformSettings.
    competitive_login_calendar_length)."""

    __tablename__ = "arena_login_rewards"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    day_number: int = Field()
    reward_config: Any = Field(default_factory=list, sa_column=sa.Column(JSONB, nullable=False, server_default="'[]'::jsonb"))
    is_milestone: bool = Field(default=False)
    icon: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ArenaLoginStreak(SQLModel, table=True):
    """Mirrors public.arena_login_streaks — one row per user."""

    __tablename__ = "arena_login_streaks"

    user_id: UUID = Field(primary_key=True, foreign_key="profiles.id")
    current_streak: int = Field(default=0)
    longest_streak: int = Field(default=0)
    last_checkin_date: Optional[date] = Field(default=None)
    grace_used_at: Optional[datetime] = Field(default=None)
    cycle_number: int = Field(default=1)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ArenaLoginRewardClaim(SQLModel, table=True):
    """Mirrors public.arena_login_reward_claims — idempotent claim ledger,
    UNIQUE(user_id, day_number, cycle_number)."""

    __tablename__ = "arena_login_reward_claims"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    day_number: int = Field()
    cycle_number: int = Field(default=1)
    claimed_at: datetime = Field(default_factory=datetime.utcnow)

    __table_args__ = (
        sa.UniqueConstraint("user_id", "day_number", "cycle_number", name="uq_arena_login_reward_claims"),
    )


class ArenaEventParticipant(SQLModel, table=True):
    """Mirrors public.arena_event_participants — opt-in/auto-tracked
    participation + per-user progress for an event's objectives.
    UNIQUE(event_id, user_id)."""

    __tablename__ = "arena_event_participants"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    event_id: UUID = Field(foreign_key="arena_events.id", index=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    progress: Any = Field(default_factory=dict, sa_column=sa.Column(JSONB, nullable=False, server_default="'{}'::jsonb"))
    completed_at: Optional[datetime] = Field(default=None)
    reward_claimed_at: Optional[datetime] = Field(default=None)
    joined_at: datetime = Field(default_factory=datetime.utcnow)

    __table_args__ = (
        sa.UniqueConstraint("event_id", "user_id", name="uq_arena_event_participants"),
    )


class ArenaCommunityProgress(SQLModel, table=True):
    """Mirrors public.arena_community_progress — one row per
    community_challenge-type ArenaEvent, holding the shared global counter."""

    __tablename__ = "arena_community_progress"

    event_id: UUID = Field(primary_key=True, foreign_key="arena_events.id")
    current_value: int = Field(default=0)
    target_value: int = Field()
    completed_at: Optional[datetime] = Field(default=None)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
