from __future__ import annotations

"""Pydantic request/response schemas for Clash Club (Competitive Arena
Phase 11, Part A). Kept in its own module rather than appended to the
already-large app/schemas/competitive.py, mirroring app/models/club.py's
own file-separation choice."""

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.models.club import (
    CLUB_MAX_MEMBERS_OPTIONS,
    CLUB_MEMBER_ROLES,
    CLUB_PRIVACY_VALUES,
)


class ClubCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    tag: str = Field(min_length=2, max_length=6)
    description: Optional[str] = None
    logo_url: Optional[str] = None
    banner_url: Optional[str] = None
    privacy: str = "public"
    max_members: Optional[int] = None  # None -> platform default
    subject_focus_id: Optional[UUID] = None
    primary_language: Optional[str] = None
    region: Optional[str] = None

    @field_validator("privacy")
    @classmethod
    def _valid_privacy(cls, v: str) -> str:
        if v not in CLUB_PRIVACY_VALUES:
            raise ValueError(f"privacy must be one of {CLUB_PRIVACY_VALUES}")
        return v

    @field_validator("max_members")
    @classmethod
    def _valid_max_members(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v not in CLUB_MAX_MEMBERS_OPTIONS:
            raise ValueError(f"max_members must be one of {CLUB_MAX_MEMBERS_OPTIONS}")
        return v


class AdminClubCreateRequest(ClubCreateRequest):
    """Admin-created club — bypasses eligibility/cost. owner_id is required
    (admins are never profiles rows themselves — see tournament_service.py's
    create_tournament docstring for the exact same precedent)."""

    owner_id: UUID


class ClubUpdateRequest(BaseModel):
    """Partial update — every field optional, only non-None fields are
    applied (mirrors tournament_service.update_tournament's convention)."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    tag: Optional[str] = Field(default=None, min_length=2, max_length=6)
    description: Optional[str] = None
    logo_url: Optional[str] = None
    banner_url: Optional[str] = None
    privacy: Optional[str] = None
    max_members: Optional[int] = None
    subject_focus_id: Optional[UUID] = None
    primary_language: Optional[str] = None
    region: Optional[str] = None

    @field_validator("privacy")
    @classmethod
    def _valid_privacy(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in CLUB_PRIVACY_VALUES:
            raise ValueError(f"privacy must be one of {CLUB_PRIVACY_VALUES}")
        return v

    @field_validator("max_members")
    @classmethod
    def _valid_max_members(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v not in CLUB_MAX_MEMBERS_OPTIONS:
            raise ValueError(f"max_members must be one of {CLUB_MAX_MEMBERS_OPTIONS}")
        return v


class ClubOut(BaseModel):
    id: UUID
    name: str
    tag: str
    logo_url: Optional[str] = None
    banner_url: Optional[str] = None
    description: Optional[str] = None
    owner_id: UUID
    owner_name: Optional[str] = None
    privacy: str
    max_members: int
    member_count: int
    subject_focus_id: Optional[UUID] = None
    primary_language: Optional[str] = None
    region: Optional[str] = None
    status: str
    suspended_at: Optional[datetime] = None
    suspended_reason: Optional[str] = None
    rating: int
    reputation: int
    wins: int
    losses: int = 0
    draws: int = 0
    matches_played: int = 0
    current_streak: int = 0
    best_streak: int = 0
    xp: int = 0
    total_ep_earned: int = 0
    last_activity_at: datetime
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ClubSearchResponse(BaseModel):
    items: List[ClubOut]
    total: int
    page: int
    size: int


class ClubMemberOut(BaseModel):
    id: UUID
    club_id: UUID
    user_id: UUID
    full_name: Optional[str] = None
    avatar_url: Optional[str] = None
    role: str
    status: str
    joined_at: datetime
    banned_at: Optional[datetime] = None
    banned_reason: Optional[str] = None

    model_config = {"from_attributes": True}


class MyClubResponse(BaseModel):
    club: Optional[ClubOut] = None
    membership: Optional[ClubMemberOut] = None


class InviteMemberRequest(BaseModel):
    user_id: UUID
    message: Optional[str] = None


class RespondInvitationRequest(BaseModel):
    accept: bool


class RoleChangeRequest(BaseModel):
    role: str

    @field_validator("role")
    @classmethod
    def _valid_role(cls, v: str) -> str:
        if v not in CLUB_MEMBER_ROLES:
            raise ValueError(f"role must be one of {CLUB_MEMBER_ROLES}")
        return v


class KickOrBanRequest(BaseModel):
    reason: Optional[str] = None


class TransferOwnershipRequest(BaseModel):
    new_owner_user_id: UUID


class ClubInvitationOut(BaseModel):
    id: UUID
    club_id: UUID
    kind: str
    user_id: UUID
    user_full_name: Optional[str] = None
    inviter_id: Optional[UUID] = None
    status: str
    message: Optional[str] = None
    created_at: datetime
    responded_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class MyClubInvitationOut(BaseModel):
    id: UUID
    club_id: UUID
    club_name: Optional[str] = None
    club_tag: Optional[str] = None
    kind: str
    user_id: UUID
    inviter_id: Optional[UUID] = None
    status: str
    message: Optional[str] = None
    created_at: datetime
    responded_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class AdminClubSuspendRequest(BaseModel):
    reason: Optional[str] = None


class AdminClubRenameRequest(BaseModel):
    name: Optional[str] = None
    tag: Optional[str] = None


class AdminRoleChangeRequest(BaseModel):
    role: str

    @field_validator("role")
    @classmethod
    def _valid_role(cls, v: str) -> str:
        if v not in CLUB_MEMBER_ROLES:
            raise ValueError(f"role must be one of {CLUB_MEMBER_ROLES}")
        return v


# ─── Part B: social layer ─────────────────────────────────────────────────

class SendChatMessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    reply_to_id: Optional[UUID] = None
    attachments: Optional[List[dict]] = None
    is_announcement: bool = False


class ChatMessageOut(BaseModel):
    id: UUID
    club_id: UUID
    user_id: UUID
    author_name: Optional[str] = None
    author_avatar_url: Optional[str] = None
    text: str
    mentions: List[UUID] = []
    attachments: List[dict] = []
    reply_to_id: Optional[UUID] = None
    is_announcement: bool
    is_pinned: bool
    deleted: bool
    created_at: datetime


class FeedEventOut(BaseModel):
    id: UUID
    club_id: UUID
    event_type: str
    actor_id: Optional[UUID] = None
    actor_name: Optional[str] = None
    data: dict = {}
    created_at: datetime


class AchievementOut(BaseModel):
    id: UUID
    code: str
    name: str
    description: Optional[str] = None
    icon: Optional[str] = None
    criteria_type: str
    criteria_value: int


class AchievementGrantOut(BaseModel):
    id: UUID
    achievement_id: UUID
    code: str
    name: str
    description: Optional[str] = None
    icon: Optional[str] = None
    granted_at: datetime


class TrophyOut(BaseModel):
    id: UUID
    club_id: UUID
    trophy_type: str
    title: str
    description: Optional[str] = None
    icon: Optional[str] = None
    awarded_at: datetime

    model_config = {"from_attributes": True}


class ClubStatisticsOut(BaseModel):
    club_id: UUID
    members: int
    active_members: int
    matches_played: int
    wins: int
    losses: int
    draws: int
    average_accuracy: float
    questions_answered: int
    win_rate: float
    current_streak: int
    best_streak: int
    season_points: int
    total_ep_earned: int
    rating: int
    reputation: int
    xp: int


class ClubAnalyticsOut(BaseModel):
    daily_activity: List[dict]
    weekly_summary: dict
    most_active_members: List[dict]
    average_online_members: int
    retention_30d_pct: float
    battle_participation_30d_pct: float


class CreateAchievementRequest(BaseModel):
    code: str
    name: str
    description: Optional[str] = None
    icon: Optional[str] = None
    criteria_type: str
    criteria_value: int = 0


class AdminReputationAdjustRequest(BaseModel):
    delta: int
    reason: str


# ─── Part C: club battles / seasons / leaderboards ────────────────────────

class CreateChallengeRequest(BaseModel):
    opponent_club_id: UUID
    challenger_roster: List[UUID]
    team_size: Optional[int] = None
    subject_ids: Optional[List[UUID]] = None
    school_level_id: Optional[UUID] = None
    difficulty: Optional[str] = None
    question_count: int = 10
    proposed_scheduled_at: Optional[datetime] = None
    message: Optional[str] = None


class RespondChallengeRequest(BaseModel):
    accept: bool
    opponent_roster: Optional[List[UUID]] = None


class ChallengeOut(BaseModel):
    id: UUID
    challenger_club_id: UUID
    opponent_club_id: UUID
    created_by: UUID
    team_size: int
    subject_ids: List[UUID] = []
    difficulty: Optional[str] = None
    question_count: int
    proposed_scheduled_at: Optional[datetime] = None
    message: Optional[str] = None
    challenger_roster: List[UUID] = []
    status: str
    match_id: Optional[UUID] = None
    created_at: datetime
    responded_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ClubBattleTeamOut(BaseModel):
    club_id: UUID
    club_name: Optional[str] = None
    club_tag: Optional[str] = None
    score: int
    players: List[dict] = []


class ClubBattleStateOut(BaseModel):
    match_id: UUID
    status: str
    team_size: int
    team_a: ClubBattleTeamOut
    team_b: ClubBattleTeamOut
    winner_club_id: Optional[UUID] = None
    mvp_user_id: Optional[UUID] = None
    scheduled_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class ClubSeasonCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None
    starts_at: datetime
    ends_at: datetime
    reset_strategy: str = "soft"
    reset_percentage: Optional[int] = None
    rules: dict = {}
    activate_now: bool = False


class ClubSeasonUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    reset_strategy: Optional[str] = None
    reset_percentage: Optional[int] = None


class ClubSeasonOut(BaseModel):
    id: UUID
    name: str
    description: Optional[str] = None
    status: str
    starts_at: datetime
    ends_at: datetime
    reset_strategy: str
    reset_percentage: Optional[int] = None
    rules: dict = {}
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
