from __future__ import annotations

"""
Clash Club — Competitive Arena Phase 11, Part A (Foundation).

Mirrors migration 089_competitive_arena_clash_club.sql. A Club is NOT
structurally a CompetitiveMatch — unlike every prior Phase 6-10 addition
(matchmaking, tournaments, battle royale), which all extend the existing
2-player/N-player match engine, a Club is a brand-new, permanent, persistent
entity (a "team") with its own lifecycle, entirely independent of any single
match. This module lives in its own file (app/models/club.py) rather than
being folded into the already-large app/models/competitive.py, mirroring
the same reasoning that led to a dedicated app/services/club/ namespace
(see app/services/club/club_service.py's module docstring).

Part C (Club Battles) will later connect a club to the existing match engine
— that's out of scope here; this module only guarantees a stable club_id
for Part C to eventually reference (no club_id column is added to
CompetitiveMatchParticipant by this phase).
"""

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlmodel import Field, SQLModel

#: club_members.role — only these 4 are usable in V1. Coach/Mentor are
#: explicitly OUT of scope (see migration 089's header) — adding them later
#: is a CHECK-constraint-only migration, not a structural change.
CLUB_MEMBER_ROLES = ["owner", "officer", "moderator", "member"]

CLUB_PRIVACY_VALUES = ["public", "private", "invite_only", "application_required"]

CLUB_STATUSES = ["active", "suspended", "deleted"]

CLUB_MEMBER_STATUSES = ["active", "banned"]

CLUB_INVITATION_KINDS = ["invite", "application"]

CLUB_INVITATION_STATUSES = ["pending", "accepted", "declined", "expired", "cancelled"]

#: Fixed capacity menu — mirrors Battle Royale's BATTLE_ROYALE_MAX_PLAYERS_
#: OPTIONS pattern (app/models/competitive.py) exactly. Each club picks one
#: of these at creation time (or an admin sets it), defaulting to
#: PlatformSettings.competitive_club_default_max_members.
CLUB_MAX_MEMBERS_OPTIONS = [20, 50, 100, 250, 500]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Club(SQLModel, table=True):
    """Mirrors public.clubs. The permanent team entity — rating/reputation
    are club-level counters, entirely separate from a player's own Arena MMR
    (CompetitiveStatistics.rating). wins/last_activity_at/member_count are
    denormalized counters maintained by app/services/club/club_service.py
    (member_count) and left for Part B/C to maintain (wins,
    last_activity_at) — added now so a later migration isn't needed just to
    support discovery sort options."""

    __tablename__ = "clubs"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(sa_column=sa.Column(sa.Text, nullable=False))
    tag: str = Field(sa_column=sa.Column(sa.Text, nullable=False))
    logo_url: Optional[str] = Field(default=None)
    banner_url: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None)
    owner_id: UUID = Field(foreign_key="profiles.id", index=True)
    privacy: str = Field(default="public")
    max_members: int = Field(default=50)
    subject_focus_id: Optional[UUID] = Field(default=None, foreign_key="subjects.id")
    primary_language: Optional[str] = Field(default=None)
    region: Optional[str] = Field(default=None, index=True)  # mirrors Profile.wilaya
    status: str = Field(default="active", index=True)
    suspended_at: Optional[datetime] = Field(default=None)
    suspended_reason: Optional[str] = Field(default=None)
    rating: int = Field(default=1000)
    reputation: int = Field(default=0)
    wins: int = Field(default=0)
    xp: int = Field(default=0)  # Phase 11 Part C — Club XP, purely cumulative/display (see migration 091)
    # Phase 11 Part C — battle statistics, mirror CompetitiveStatistics'
    # raw-counter shape, maintained by app/services/club/battle_service.py.
    matches_played: int = Field(default=0)
    losses: int = Field(default=0)
    draws: int = Field(default=0)
    current_streak: int = Field(default=0)
    best_streak: int = Field(default=0)
    total_ep_earned: int = Field(default=0)
    member_count: int = Field(default=1)
    last_activity_at: datetime = Field(default_factory=_now)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class ClubMember(SQLModel, table=True):
    """Mirrors public.club_members. UNIQUE (club_id, user_id). Exactly one
    'owner' row per club is enforced at the DB level via a partial unique
    index on (club_id) WHERE role='owner' (see migration 089) — application
    code never needs to re-derive this invariant. A banned member's row is
    kept (status='banned', not deleted) so a rejoin attempt can be detected
    and rejected (see club_service.join_club)."""

    __tablename__ = "club_members"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    club_id: UUID = Field(foreign_key="clubs.id", index=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    role: str = Field(default="member")
    joined_at: datetime = Field(default_factory=_now)
    status: str = Field(default="active")
    banned_at: Optional[datetime] = Field(default=None)
    banned_reason: Optional[str] = Field(default=None)
    banned_by: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    created_at: datetime = Field(default_factory=_now)


class ClubInvitation(SQLModel, table=True):
    """Mirrors public.club_invitations. Unifies BOTH directions via `kind`:
    an owner/officer inviting a player (kind='invite', inviter_id
    populated), or a player applying to join an application_required club
    (kind='application', inviter_id NULL — the applicant IS user_id). A
    partial unique index prevents a duplicate PENDING row for the same
    (club_id, user_id) — see migration 089."""

    __tablename__ = "club_invitations"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    club_id: UUID = Field(foreign_key="clubs.id", index=True)
    kind: str = Field()
    user_id: UUID = Field(foreign_key="profiles.id", index=True)  # the player (invitee or applicant)
    inviter_id: Optional[UUID] = Field(default=None, foreign_key="profiles.id")  # NULL for an application
    status: str = Field(default="pending", index=True)
    message: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=_now)
    responded_at: Optional[datetime] = Field(default=None)
    expires_at: Optional[datetime] = Field(default=None)


# ─── Part B: social layer (migration 090) ────────────────────────────────────

class ClubChatMessage(SQLModel, table=True):
    """Mirrors public.club_chat_messages. A dedicated many-member room chat,
    distinct from both the DM messaging system (app/models/conversation.py)
    and the per-match spectator chat (CompetitiveMatchChat) — see migration
    090's header for the full reasoning. Moderation reuses the existing
    CompetitiveBlockedWord table verbatim (see app/services/club/
    chat_service.py)."""

    __tablename__ = "club_chat_messages"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    club_id: UUID = Field(foreign_key="clubs.id", index=True)
    user_id: UUID = Field(foreign_key="profiles.id")
    text: str = Field()
    mentions: list = Field(
        default_factory=list,
        sa_column=sa.Column(ARRAY(sa.UUID(as_uuid=True)), nullable=False, server_default="{}"),
    )
    attachments: Any = Field(
        default_factory=list,
        sa_column=sa.Column(JSONB, nullable=False, server_default="'[]'::jsonb"),
    )
    reply_to_id: Optional[UUID] = Field(default=None, foreign_key="club_chat_messages.id")
    is_announcement: bool = Field(default=False)
    is_pinned: bool = Field(default=False)
    pinned_at: Optional[datetime] = Field(default=None)
    pinned_by: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    deleted_at: Optional[datetime] = Field(default=None)
    deleted_by: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    created_at: datetime = Field(default_factory=_now)


class ClubFeedEvent(SQLModel, table=True):
    """Mirrors public.club_feed_events. Permanent, append-only activity feed
    — every important club action generates one (see club_service.py's
    log_event, which ALSO writes to AuditLog — the two are complementary:
    AuditLog is the admin-facing audit trail, ClubFeedEvent is the
    member-facing public feed; club_feed_service.record() is called
    alongside, never instead of, log_event())."""

    __tablename__ = "club_feed_events"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    club_id: UUID = Field(foreign_key="clubs.id", index=True)
    event_type: str = Field()
    actor_id: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    data: Any = Field(
        default_factory=dict,
        sa_column=sa.Column(JSONB, nullable=False, server_default="'{}'::jsonb"),
    )
    created_at: datetime = Field(default_factory=_now)


#: club_achievements.criteria_type — the counter achievement_service checks
#: against criteria_value after an event that could move it.
CLUB_ACHIEVEMENT_CRITERIA_TYPES = [
    "wins_total", "perfect_match", "member_count", "rating_threshold",
    "season_champion", "tournament_champion", "reputation_threshold",
]


class ClubAchievement(SQLModel, table=True):
    """Mirrors public.club_achievements — the admin-configurable catalogue,
    seeded by migration 090 with the spec's own examples."""

    __tablename__ = "club_achievements"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    code: str = Field()
    name: str = Field()
    description: Optional[str] = Field(default=None)
    icon: Optional[str] = Field(default=None)
    criteria_type: str = Field()
    criteria_value: int = Field(default=0)
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=_now)


class ClubAchievementGrant(SQLModel, table=True):
    """Mirrors public.club_achievement_grants — one row per (club,
    achievement) actually earned; UNIQUE(club_id, achievement_id)."""

    __tablename__ = "club_achievement_grants"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    club_id: UUID = Field(foreign_key="clubs.id", index=True)
    achievement_id: UUID = Field(foreign_key="club_achievements.id")
    granted_at: datetime = Field(default_factory=_now)


#: club_trophies.trophy_type
CLUB_TROPHY_TYPES = ["season", "tournament", "club_battle", "special_event"]


class ClubTrophy(SQLModel, table=True):
    """Mirrors public.club_trophies — the permanent Trophy Cabinet.
    season_id/tournament_id/battle_id are loose, un-FK'd pointers (see
    migration 090's header) — trophy_type discriminates which one, if any,
    is populated."""

    __tablename__ = "club_trophies"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    club_id: UUID = Field(foreign_key="clubs.id", index=True)
    trophy_type: str = Field()
    title: str = Field()
    description: Optional[str] = Field(default=None)
    icon: Optional[str] = Field(default=None)
    season_id: Optional[UUID] = Field(default=None)
    tournament_id: Optional[UUID] = Field(default=None)
    battle_id: Optional[UUID] = Field(default=None)
    awarded_at: datetime = Field(default_factory=_now)


class ClubReputationEvent(SQLModel, table=True):
    """Mirrors public.club_reputation_events — the audit ledger behind
    clubs.reputation (a running counter since migration 089)."""

    __tablename__ = "club_reputation_events"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    club_id: UUID = Field(foreign_key="clubs.id", index=True)
    delta: int = Field()
    reason: str = Field()
    actor_id: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    created_at: datetime = Field(default_factory=_now)


class ClubDailyActivity(SQLModel, table=True):
    """Mirrors public.club_daily_activity — a per-club/per-day rollup,
    populated by a nightly Celery task (app/workers/club_tasks.py), so
    analytics_service can answer 'daily/weekly activity'/'retention' in
    O(days) instead of scanning club_chat_messages/club_feed_events at full
    scale (spec's Performance section: 'tens of thousands of clubs')."""

    __tablename__ = "club_daily_activity"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    club_id: UUID = Field(foreign_key="clubs.id", index=True)
    activity_date: Any = Field(sa_column=sa.Column(sa.Date, nullable=False))
    message_count: int = Field(default=0)
    active_member_count: int = Field(default=0)
    battles_played: int = Field(default=0)
    new_members: int = Field(default=0)


# ─── Part C: club battles / rating / seasons (migration 091) ────────────────

CLUB_BATTLE_TEAM_SIZES = [3, 5, 10]

CLUB_BATTLE_CHALLENGE_STATUSES = ["pending", "accepted", "declined", "cancelled", "expired"]


class ClubBattleChallenge(SQLModel, table=True):
    """Mirrors public.club_battle_challenges — the club-to-club challenge/
    acceptance handshake (see migration 091's header for why this is a
    dedicated table, not an overload of ClubInvitation)."""

    __tablename__ = "club_battle_challenges"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    challenger_club_id: UUID = Field(foreign_key="clubs.id", index=True)
    opponent_club_id: UUID = Field(foreign_key="clubs.id", index=True)
    created_by: UUID = Field(foreign_key="profiles.id")
    team_size: int = Field(default=3)
    subject_ids: list = Field(
        default_factory=list,
        sa_column=sa.Column(ARRAY(sa.UUID(as_uuid=True)), nullable=False, server_default="{}"),
    )
    school_level_id: Optional[UUID] = Field(default=None, foreign_key="levels.id")
    difficulty: Optional[str] = Field(default=None)
    question_count: int = Field(default=10)
    proposed_scheduled_at: Optional[datetime] = Field(default=None)
    message: Optional[str] = Field(default=None)
    challenger_roster: list = Field(
        default_factory=list,
        sa_column=sa.Column(ARRAY(sa.UUID(as_uuid=True)), nullable=False, server_default="{}"),
    )
    status: str = Field(default="pending")
    match_id: Optional[UUID] = Field(default=None, foreign_key="competitive_matches.id")
    created_at: datetime = Field(default_factory=_now)
    responded_at: Optional[datetime] = Field(default=None)
    expires_at: Optional[datetime] = Field(default=None)


class ClubBattleMatch(SQLModel, table=True):
    """Mirrors public.club_battle_matches — 1:1 companion of a
    CompetitiveMatch (match_type='club_battle'), same convention as
    CompetitiveBattleRoyaleMatch (PK IS match_id)."""

    __tablename__ = "club_battle_matches"

    match_id: UUID = Field(foreign_key="competitive_matches.id", primary_key=True)
    challenge_id: Optional[UUID] = Field(default=None, foreign_key="club_battle_challenges.id")
    club_a_id: UUID = Field(foreign_key="clubs.id")
    club_b_id: UUID = Field(foreign_key="clubs.id")
    team_size: int = Field()
    club_a_score: int = Field(default=0)
    club_b_score: int = Field(default=0)
    winner_club_id: Optional[UUID] = Field(default=None, foreign_key="clubs.id")
    mvp_user_id: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    tie_break_rules: Any = Field(
        default_factory=dict,
        sa_column=sa.Column(JSONB, nullable=False, server_default="'{}'::jsonb"),
    )
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class ClubSeason(SQLModel, table=True):
    """Mirrors public.club_seasons — entirely independent season track from
    the player-level CompetitiveSeason. Only one row may have status='active'
    at a time (DB partial unique index, see migration 091)."""

    __tablename__ = "club_seasons"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field()
    description: Optional[str] = Field(default=None)
    status: str = Field(default="upcoming")
    starts_at: datetime = Field()
    ends_at: datetime = Field()
    reset_strategy: str = Field(default="soft")
    reset_percentage: Optional[int] = Field(default=None)
    rules: Any = Field(
        default_factory=dict,
        sa_column=sa.Column(JSONB, nullable=False, server_default="'{}'::jsonb"),
    )
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class ClubSeasonHistory(SQLModel, table=True):
    """Mirrors public.club_season_history — the permanent per-club,
    per-season archive. Deliberately NO foreign_key/cascade on club_id (see
    migration 091's header) — this row must outlive the club itself."""

    __tablename__ = "club_season_history"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    season_id: UUID = Field(foreign_key="club_seasons.id")
    club_id: UUID = Field(index=True)
    club_name: str = Field()
    final_rank: Optional[int] = Field(default=None)
    final_rating: int = Field()
    wins: int = Field(default=0)
    losses: int = Field(default=0)
    draws: int = Field(default=0)
    rewards: Any = Field(
        default_factory=list,
        sa_column=sa.Column(JSONB, nullable=False, server_default="'[]'::jsonb"),
    )
    achievements: Any = Field(
        default_factory=list,
        sa_column=sa.Column(JSONB, nullable=False, server_default="'[]'::jsonb"),
    )
    created_at: datetime = Field(default_factory=_now)


class ClubRatingHistory(SQLModel, table=True):
    """Mirrors public.club_rating_history — ledger behind clubs.rating,
    mirrors CompetitiveRatingHistory's own shape."""

    __tablename__ = "club_rating_history"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    club_id: UUID = Field(foreign_key="clubs.id", index=True)
    match_id: Optional[UUID] = Field(default=None, foreign_key="competitive_matches.id")
    opponent_club_id: Optional[UUID] = Field(default=None, foreign_key="clubs.id")
    season_id: Optional[UUID] = Field(default=None, foreign_key="club_seasons.id")
    result: str = Field()  # win|loss|draw|admin_adjustment
    rating_before: int = Field()
    rating_after: int = Field()
    delta: int = Field()
    created_at: datetime = Field(default_factory=_now)
