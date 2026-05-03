from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlmodel import Field, SQLModel

# Re-exports for backward compat (routers import these from this module)
from app.models.enums import (  # noqa: F401
    PaymentMethodType,
    PaymentStatus,
    WithdrawalStatus,
)


class PaymentMethod(SQLModel, table=True):
    """
    Mirrors public.payment_methods.
    Stores a saved payment instrument for a user (tokenized CIB, Edahabia, etc.)
    """

    __tablename__ = "payment_methods"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    type: str = Field()                                  # public.payment_method_type
    label: Optional[str] = Field(default=None)
    last4: Optional[str] = Field(default=None)
    token: Optional[str] = Field(default=None)
    is_default: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Payment(SQLModel, table=True):
    """
    Mirrors public.payments.
    booking_id FK defined here (non-circular direction).
    The reverse FK (bookings.payment_id → payments.id) is DB-level only (Supabase SQL).
    """

    __tablename__ = "payments"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    booking_id: Optional[UUID] = Field(default=None, foreign_key="bookings.id")
    amount: int = Field()                                # DZD
    currency: str = Field(default="DZD")
    method_id: Optional[UUID] = Field(default=None, foreign_key="payment_methods.id")
    method_type: Optional[str] = Field(default=None)    # public.payment_method_type
    status: str = Field(default="pending")               # public.payment_status
    provider_ref: Optional[str] = Field(default=None)
    paid_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Invoice(SQLModel, table=True):
    """
    Mirrors public.invoices.
    Auto-generated PDF receipt linked to a payment.
    UNIQUE on payment_id — one invoice per payment.
    """

    __tablename__ = "invoices"
    __table_args__ = (
        sa.UniqueConstraint("payment_id", name="uq_invoice_payment"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    payment_id: UUID = Field(foreign_key="payments.id", index=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    number: str = Field(sa_column=sa.Column(sa.String, unique=True, nullable=False))
    pdf_url: Optional[str] = Field(default=None)
    amount_ht: Optional[int] = Field(default=None)
    vat: Optional[int] = Field(default=None)
    amount_ttc: Optional[int] = Field(default=None)
    issued_at: datetime = Field(default_factory=datetime.utcnow)


class Wallet(SQLModel, table=True):
    """
    Mirrors public.wallets.
    Teacher's DZD earnings wallet. One wallet per user (PK = user_id).
    """

    __tablename__ = "wallets"

    user_id: UUID = Field(primary_key=True, foreign_key="profiles.id")
    balance: int = Field(default=0)
    total_earned: int = Field(default=0)
    pending_amount: int = Field(default=0)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Withdrawal(SQLModel, table=True):
    """
    Mirrors public.withdrawals.
    Teacher requests a DZD payout to their bank account.
    """

    __tablename__ = "withdrawals"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    teacher_id: UUID = Field(foreign_key="profiles.id", index=True)
    amount: int = Field()                                # DZD
    status: str = Field(default="pending")               # public.withdrawal_status
    iban: Optional[str] = Field(default=None)
    requested_at: datetime = Field(default_factory=datetime.utcnow)
    processed_at: Optional[datetime] = Field(default=None)
