from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from sqlmodel import Field, SQLModel


class TeacherWalletTransaction(SQLModel, table=True):
    """
    Mirrors public.teacher_wallet_transactions (migration 111).
    Ledger for teacher_profiles.wallet_balance_dzd — same pattern as
    kp_transactions/kp_balances (migration 109), for the same reason: a
    plain mutable balance column with no history made "why is this
    teacher's wallet what it is" unanswerable without grepping logs.

    amount > 0 = credit (session payout, a rejected withdrawal refunded);
    amount < 0 = debit (withdrawal request, admin clawback on a rejected
    session validation).
    transaction_type: credit/debit = ordinary. adjustment = manual admin
    correction (actor_id required). reversal = undoes a prior transaction
    (see ref_type/ref_id).
    source: 'session_payout' | 'withdrawal_request' | 'withdrawal_rejected'
    | 'admin_adjustment' | 'clawback'.
    ref_type/ref_id: polymorphic reference to the triggering entity — also
    used, together with transaction_type, for idempotency (see
    app/services/wallet.py and migration 111's unique index).
    actor_id: who caused this when it wasn't the teacher themself (admin
    adjustments, clawbacks). Null for an ordinary session-payout credit or
    a teacher-initiated withdrawal debit.
    """

    __tablename__ = "teacher_wallet_transactions"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    teacher_id: UUID = Field(foreign_key="profiles.id", index=True)
    amount: int = Field()
    transaction_type: str = Field()                      # public.wallet_transaction_type
    source: str = Field()
    label: Optional[str] = Field(default=None)
    ref_type: Optional[str] = Field(default=None)
    ref_id: Optional[UUID] = Field(default=None)
    actor_id: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
