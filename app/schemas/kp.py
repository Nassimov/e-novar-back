from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class KpBalanceResponse(BaseModel):
    user_id: UUID
    balance: int
    total_earned: int
    week_earned: int
    level: int
    xp: int
    next_level_at: int
    streak_days: int

    model_config = {"from_attributes": True}


class KpTransactionResponse(BaseModel):
    id: UUID
    user_id: UUID
    label: str
    source: str
    amount: int
    balance_after: int
    created_at: datetime

    model_config = {"from_attributes": True}


class KpSpendRequest(BaseModel):
    amount: int = Field(gt=0)
    label: str
    reward_id: Optional[str] = None


class BadgeResponse(BaseModel):
    id: str
    name: str
    description: str
    icon_url: Optional[str] = None
    required_level: int
    kp_cost: int
    is_unlocked: bool = False


class ChallengeResponse(BaseModel):
    id: UUID
    title: str
    description: str
    reward_kp: int
    proof_type: str
    audience: str
    is_active: bool
    ends_at: Optional[datetime] = None
    user_submitted: bool = False
    user_status: Optional[str] = None

    model_config = {"from_attributes": True}


class LeaderboardEntry(BaseModel):
    rank: int
    user_id: UUID
    full_name: str
    avatar_url: Optional[str] = None
    level: int
    xp: int
    week_earned: int
