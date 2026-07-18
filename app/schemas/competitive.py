from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.models.competitive import INVITATION_STATUSES, MATCH_TYPES


class MatchCreateRequest(BaseModel):
    """Creates a match draft. For a duel, pass opponent_code to immediately
    send an invitation (see app/routers/competitive/matches.py) — the match
    itself is always created first regardless of match_type."""

    match_type: str
    subject_ids: List[UUID] = Field(default_factory=list)
    school_level_id: Optional[UUID] = None
    difficulty: Optional[str] = None
    question_count: int = 10
    visibility: str = "private"
    scheduled_at: Optional[datetime] = None
    opponent_code: Optional[str] = None  # student_code of the invitee (duel only)

    @field_validator("match_type")
    @classmethod
    def _valid_match_type(cls, v: str) -> str:
        if v not in MATCH_TYPES:
            raise ValueError(f"match_type must be one of {MATCH_TYPES}")
        return v

    @field_validator("visibility")
    @classmethod
    def _valid_visibility(cls, v: str) -> str:
        if v not in ("private", "public"):
            raise ValueError("visibility must be 'private' or 'public'")
        return v

    @field_validator("question_count")
    @classmethod
    def _valid_question_count(cls, v: int) -> int:
        if not (1 <= v <= 50):
            raise ValueError("question_count must be between 1 and 50")
        return v


class ParticipantResponse(BaseModel):
    id: UUID
    user_id: UUID
    full_name: Optional[str] = None
    avatar_url: Optional[str] = None
    role: str
    is_ready: bool
    result: Optional[str] = None
    score: int

    model_config = {"from_attributes": True}


class MatchResponse(BaseModel):
    id: UUID
    match_type: str
    status: str
    creator_id: UUID
    subject_ids: List[UUID]
    school_level_id: Optional[UUID] = None
    difficulty: Optional[str] = None
    question_count: int
    visibility: str
    max_players: int
    scheduled_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    created_at: datetime
    participants: List[ParticipantResponse] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class InvitationCreateRequest(BaseModel):
    match_id: UUID
    opponent_code: str


class InvitationResponse(BaseModel):
    id: UUID
    match_id: UUID
    inviter_id: UUID
    invitee_id: UUID
    inviter_name: Optional[str] = None
    invitee_name: Optional[str] = None
    status: str
    expires_at: datetime
    responded_at: Optional[datetime] = None
    created_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("status")
    @classmethod
    def _valid_status(cls, v: str) -> str:
        if v not in INVITATION_STATUSES:
            raise ValueError(f"status must be one of {INVITATION_STATUSES}")
        return v


class StatisticsResponse(BaseModel):
    user_id: UUID
    matches_played: int
    wins: int
    losses: int
    draws: int
    accuracy_pct: float
    avg_response_time_ms: Optional[int] = None
    current_streak: int
    longest_streak: int
    favorite_subject_id: Optional[UUID] = None
    favorite_difficulty: Optional[str] = None
    perfect_matches: int
    rank_tier: str
    best_rank_tier: str
    rating: int
    tournament_wins: int
    club_wins: int
    battle_royale_wins: int
    updated_at: datetime

    model_config = {"from_attributes": True}
