from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID, uuid4

from sqlmodel import Field, SQLModel


class KpSource(str, Enum):
    booking = "booking"
    challenge = "challenge"
    homework = "homework"
    referral = "referral"
    streak = "streak"
    reward = "reward"
    admin = "admin"
    spend = "spend"
    review = "review"


class KpAccount(SQLModel, table=True):
    __tablename__ = "kp_accounts"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="users.id", unique=True, index=True)
    balance: int = Field(default=0)
    total_earned: int = Field(default=0)
    week_earned: int = Field(default=0)
    level: int = Field(default=1)
    xp: int = Field(default=0)
    next_level_at: int = Field(default=500)
    streak_days: int = Field(default=0)
    last_activity_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class KpTransaction(SQLModel, table=True):
    __tablename__ = "kp_transactions"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="users.id", index=True)
    label: str = Field()
    source: KpSource = Field(default=KpSource.admin)
    amount: int = Field()  # positive = earn, negative = spend
    balance_after: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)
