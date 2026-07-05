from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class Referral(SQLModel, table=True):
    """
    One row per (referrer, referee) pair.
    status: pending → registered (referee applied code) → validated (first session done).
    kp_awarded: KP awarded to the referrer at validation.
    referee_role: role of the referee at apply-time, determines reward tier.
    """

    __tablename__ = "referrals"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    referrer_id: UUID = Field(foreign_key="profiles.id", index=True)
    referee_id: Optional[UUID] = Field(default=None, foreign_key="profiles.id", index=True)
    # code is NOT unique — the same user code can appear on multiple rows
    code: str = Field(sa_column=sa.Column(sa.String, nullable=False, index=True))
    email_invited: Optional[str] = Field(default=None)
    status: str = Field(default="registered")            # registered | validated
    referee_role: str = Field(default="student")         # student | teacher | parent
    kp_awarded: int = Field(default=0)                   # KP credited to referrer
    created_at: datetime = Field(default_factory=datetime.utcnow)
    validated_at: Optional[datetime] = Field(default=None)
