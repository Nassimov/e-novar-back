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

    Display/reference metadata only — never the full card number, CVV or
    expiry (migration 056). Card rails (cib/edahabia/visa) carry `last4`;
    the wallet rail (baridimob, identified by phone number, not a card)
    carries `phone` instead; the RIB rails (rib_cib/rib_edahabia — a
    student's or teacher's own bank account, used for manual-transfer
    reconciliation, not automated debit — see migration 068) carry `rib`.
    """

    __tablename__ = "payment_methods"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    type: str = Field()                                  # public.payment_method_type
    label: Optional[str] = Field(default=None)
    last4: Optional[str] = Field(default=None)
    token: Optional[str] = Field(default=None)
    holder_name: Optional[str] = Field(default=None)
    phone: Optional[str] = Field(default=None)           # baridimob only
    bank_name: Optional[str] = Field(default=None)
    rib: Optional[str] = Field(default=None)             # rib_cib / rib_edahabia only
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


class TeacherPayout(SQLModel, table=True):
    """
    Mirrors public.teacher_payouts.
    Teacher requests to convert EP → DZD. Admin approves and sets dzd_amount.
    Requires at least 1 completed session (enforced at API level).
    """

    __tablename__ = "teacher_payouts"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    teacher_id: UUID = Field(foreign_key="profiles.id", index=True)
    ep_amount: int = Field()                             # EP à convertir
    dzd_amount: Optional[int] = Field(default=None)     # DZD fixé par l'admin
    iban: str = Field()
    bank_holder: str = Field()
    status: str = Field(default="pending")               # public.withdrawal_status
    admin_note: Optional[str] = Field(default=None)
    requested_at: datetime = Field(default_factory=datetime.utcnow)
    processed_at: Optional[datetime] = Field(default=None)


# Legacy aliases — kept for import compatibility during transition
Withdrawal = TeacherPayout
