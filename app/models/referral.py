from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class Referral(SQLModel, table=True):
    """
    Mirrors public.referrals.
    referrer_id invites referee_id (or an email) via a unique code.
    status: pending → registered (referee signed up) → validated (first booking done).
    kp_awarded tracks how many KP were given to the referrer on validation.
    """

    __tablename__ = "referrals"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    referrer_id: UUID = Field(foreign_key="profiles.id", index=True)
    referee_id: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    code: str = Field(sa_column=sa.Column(sa.String, unique=True, nullable=False))
    email_invited: Optional[str] = Field(default=None)
    status: str = Field(default="pending")               # public.referral_status
    kp_awarded: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    validated_at: Optional[datetime] = Field(default=None)
