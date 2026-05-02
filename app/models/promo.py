from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from sqlmodel import Field, SQLModel


class PromoCode(SQLModel, table=True):
    __tablename__ = "promo_codes"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    code: str = Field(unique=True, index=True)
    discount_percent: Optional[int] = Field(default=None)  # e.g. 20 for 20%
    discount_dzd: Optional[int] = Field(default=None)  # fixed DZD discount
    max_uses: int = Field(default=100)
    used_count: int = Field(default=0)
    expires_at: Optional[datetime] = Field(default=None)
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Referral(SQLModel, table=True):
    __tablename__ = "referrals"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    referrer_id: UUID = Field(foreign_key="users.id", index=True)
    referee_id: Optional[UUID] = Field(default=None, foreign_key="users.id")
    code: str = Field(unique=True, index=True)
    status: str = Field(default="pending")  # pending/completed
    kp_awarded: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Favorite(SQLModel, table=True):
    __tablename__ = "favorites"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    student_id: UUID = Field(foreign_key="users.id", index=True)
    teacher_id: UUID = Field(foreign_key="users.id", index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
