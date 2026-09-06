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
    amount: int = Field()                                # smallest whole unit of `currency` — DZD has no subunit in practice, EUR is stored in cents (see app/services/stripe.py)
    currency: str = Field(default="DZD")
    method_id: Optional[UUID] = Field(default=None, foreign_key="payment_methods.id")
    method_type: Optional[str] = Field(default=None)    # public.payment_method_type
    status: str = Field(default="pending")               # public.payment_status
    provider: Optional[str] = Field(default=None)        # "chargily" | "stripe" — migration 100
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
    Two request sources share this same admin-facing queue (see migration
    102): 'ep_conversion' (teacher converts EP -> DZD; admin sets dzd_amount
    on approval, EP deducted via kp_transactions) and 'wallet' (teacher cashes
    out real session-earnings from wallet_balance_dzd — dzd_amount is already
    fixed at request time, the wallet was already deducted then too, so a
    reject on this source must refund it — see admin/content.py's
    process_withdrawal). Requires at least 1 completed session (enforced at
    API level, ep_conversion only).
    """

    __tablename__ = "teacher_payouts"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    teacher_id: UUID = Field(foreign_key="profiles.id", index=True)
    source: str = Field(default="ep_conversion")         # 'wallet' | 'ep_conversion'
    ep_amount: int = Field(default=0)                    # EP à convertir — 0 for a 'wallet' request
    dzd_amount: Optional[int] = Field(default=None)     # fixé par l'admin — despite the name, denominates
                                                          # whatever `currency` says (kept unrenamed, see
                                                          # migration 100 — a live financial column rename
                                                          # is a separate, riskier change with no functional
                                                          # benefit right now). Already known for 'wallet'.
    currency: str = Field(default="DZD")                 # ISO 4217 — source of truth for dzd_amount's unit
    payout_rail: Optional[str] = Field(default=None)    # 'bank' | 'baridimob' — teacher's rail at request time
    iban: Optional[str] = Field(default=None)
    bank_holder: Optional[str] = Field(default=None)
    payout_phone: Optional[str] = Field(default=None)   # BaridiMob destination
    status: str = Field(default="pending")               # public.withdrawal_status
    admin_note: Optional[str] = Field(default=None)
    requested_at: datetime = Field(default_factory=datetime.utcnow)
    processed_at: Optional[datetime] = Field(default=None)


# Legacy aliases — kept for import compatibility during transition
Withdrawal = TeacherPayout
