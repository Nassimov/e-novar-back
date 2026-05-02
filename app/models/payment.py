from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID, uuid4

from sqlmodel import Field, SQLModel


class PaymentMethod(str, Enum):
    cib = "cib"
    edahabia = "edahabia"
    transfer = "transfer"
    cash = "cash"
    stripe = "stripe"


class PaymentStatus(str, Enum):
    pending = "pending"
    completed = "completed"
    failed = "failed"
    refunded = "refunded"


class Payment(SQLModel, table=True):
    __tablename__ = "payments"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="users.id", index=True)
    booking_id: Optional[UUID] = Field(default=None, foreign_key="bookings.id", index=True)
    amount: int = Field()  # DZD
    currency: str = Field(default="DZD")
    method: PaymentMethod = Field(default=PaymentMethod.transfer)
    status: PaymentStatus = Field(default=PaymentStatus.pending)
    stripe_payment_intent_id: Optional[str] = Field(default=None)
    reference: Optional[str] = Field(default=None)
    invoice_url: Optional[str] = Field(default=None)
    notes: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
