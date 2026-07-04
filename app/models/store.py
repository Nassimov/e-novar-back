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
    effect_type: if set, the item is auto-processed on claim (no admin action needed).
    effect_config: JSONB payload consumed by the effect handler (e.g. {"duration_hours": 24}).
    """

    __tablename__ = "store_items"

    id: str = Field(primary_key=True)
    category: str = Field()
    name: str = Field()
    description: Optional[str] = Field(default=None)
    cost: int = Field()
    level_required: int = Field(default=1)
    icon: Optional[str] = Field(default=None)
    badge: Optional[str] = Field(default=None)
    stock: Optional[int] = Field(default=None)           # None = unlimited
    active: bool = Field(default=True)
    effect_type: Optional[str] = Field(default=None)     # e.g. "ep_boost_2x_24h"
    effect_config: Optional[Any] = Field(
        default=None,
        sa_column=sa.Column(JSONB, nullable=True, server_default="'{}'::jsonb"),
    )


class RewardClaim(SQLModel, table=True):
    """
    Mirrors public.reward_claims.
    Records a user spending KP to claim a store item.
    status: pending (awaiting admin) | approved (auto-processed or admin-approved) |
            delivered (physical shipped) | cancelled.
    shipping_info: jsonb — address for physical rewards.
    """

    __tablename__ = "reward_claims"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    item_id: str = Field(foreign_key="store_items.id", index=True)
    cost: int = Field()
    status: str = Field(default="pending")
    shipping_info: Optional[Any] = Field(
        default=None,
        sa_column=sa.Column(JSONB, nullable=True, server_default="'{}'::jsonb"),
    )
    claimed_at: datetime = Field(default_factory=datetime.utcnow)
    processed_at: Optional[datetime] = Field(default=None)


class UserEntitlement(SQLModel, table=True):
    """
    Tracks the concrete in-app effect granted by a store claim.
    effect_type drives which feature is unlocked (e.g. ep_boost_2x_24h).
    effect_config stores effect-specific parameters.
    status: active | used | expired.
    """

    __tablename__ = "user_entitlements"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    claim_id: UUID = Field(foreign_key="reward_claims.id", index=True)
    effect_type: str = Field()
    effect_config: Optional[Any] = Field(
        default=None,
        sa_column=sa.Column(JSONB, nullable=True, server_default="'{}'::jsonb"),
    )
    status: str = Field(default="active")       # active | used | expired
    expires_at: Optional[datetime] = Field(default=None)
    activated_at: datetime = Field(default_factory=datetime.utcnow)
    used_at: Optional[datetime] = Field(default=None)
