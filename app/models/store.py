from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class StoreItem(SQLModel, table=True):
    """
    Mirrors public.store_items.
    Text PK (slug-like, e.g. 'hint_powerup_x1').
    category: powerups | digital | physical | services | travel.
    cost: KP price; level_required: minimum KpBalance.level to unlock.
    """

    __tablename__ = "store_items"

    id: str = Field(primary_key=True)
    category: str = Field()                              # public.store_category
    name: str = Field()
    description: Optional[str] = Field(default=None)
    cost: int = Field()                                  # KP cost
    level_required: int = Field(default=1)
    icon: Optional[str] = Field(default=None)
    badge: Optional[str] = Field(default=None)
    stock: Optional[int] = Field(default=None)           # None = unlimited
    active: bool = Field(default=True)


class RewardClaim(SQLModel, table=True):
    """
    Mirrors public.reward_claims.
    Records a student spending KP to claim a store item.
    shipping_info: jsonb — address for physical rewards.
    """

    __tablename__ = "reward_claims"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    item_id: str = Field(foreign_key="store_items.id", index=True)
    cost: int = Field()                                  # KP deducted
    status: str = Field(default="pending")               # public.claim_status
    shipping_info: Optional[Any] = Field(
        default=None,
        sa_column=sa.Column(JSONB, nullable=True, server_default="'{}'::jsonb"),
    )
    claimed_at: datetime = Field(default_factory=datetime.utcnow)
    processed_at: Optional[datetime] = Field(default=None)
