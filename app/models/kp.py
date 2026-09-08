from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlmodel import Field, SQLModel

# Re-exports for backward compat (routers use KpAccount and KpSource from here)
from app.models.enums import KpSource, KpTransactionType  # noqa: F401


class KpBalance(SQLModel, table=True):
    """
    Mirrors public.kp_balances.
    PK = user_id — one balance row per user.
    Maintained automatically by the apply_kp_transaction() trigger in Supabase.
    xp drives level progression; next_level_at is the XP threshold for the next level.
    """

    __tablename__ = "kp_balances"
    # Last-resort floor, independent of application code -- see migration
    # 109. The real safety comes from the row lock app/services/kp.py now
    # takes before checking balance; this only catches what that misses.
    __table_args__ = (
        sa.CheckConstraint("balance >= 0", name="chk_kp_balance_non_negative"),
    )

    user_id: UUID = Field(primary_key=True, foreign_key="profiles.id")
    balance: int = Field(default=0)
    total_earned: int = Field(default=0)
    week_earned: int = Field(default=0)
    level: int = Field(default=1)
    xp: int = Field(default=0)
    next_level_at: int = Field(default=200)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class KpTransaction(SQLModel, table=True):
    """
    Mirrors public.kp_transactions.
    source: public.kp_source — reason for the KP award/deduction.
    transaction_type: public.kp_transaction_type — nature of the row (see
    KpTransactionType). Distinct from `source`: source is WHY (homework,
    referral, competitive...), transaction_type is WHAT KIND (earn, spend,
    adjustment, reversal...).
    amount > 0 = earn/bonus/adjustment-up; amount < 0 = spend/reversal/
    adjustment-down.
    ref_type / ref_id: polymorphic reference to the triggering entity — also
    used, together with transaction_type, to make an award/spend idempotent
    per event (see app/services/kp.py and migration 109's unique index).
    actor_id: who caused this when it wasn't the user themself (admin
    adjustments, system reversals). idempotency_key: caller-supplied dedup
    key for a spend with no natural ref_id.
    """

    __tablename__ = "kp_transactions"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    amount: int = Field()
    source: str = Field()                                # public.kp_source
    transaction_type: str = Field(default="earn")        # public.kp_transaction_type
    actor_id: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    idempotency_key: Optional[str] = Field(default=None)
    label: Optional[str] = Field(default=None)
    ref_type: Optional[str] = Field(default=None)
    ref_id: Optional[UUID] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class KpLevel(SQLModel, table=True):
    """
    Mirrors public.kp_levels.
    Static table defining level names, XP thresholds, and perks.
    Integer PK (not UUID).
    """

    __tablename__ = "kp_levels"

    num: int = Field(primary_key=True)
    name: str = Field()
    min_xp: int = Field()
    perks: Optional[str] = Field(default=None)


# Legacy alias — old routers/services use KpAccount
KpAccount = KpBalance
