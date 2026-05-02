from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel


class PaymentInitiate(BaseModel):
    booking_id: UUID
    method: str  # cib/edahabia/transfer/cash/stripe
    amount: int
    currency: str = "DZD"


class PaymentResponse(BaseModel):
    id: UUID
    user_id: UUID
    booking_id: Optional[UUID] = None
    amount: int
    currency: str
    method: str
    status: str
    stripe_payment_intent_id: Optional[str] = None
    reference: Optional[str] = None
    invoice_url: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class TransferVerify(BaseModel):
    payment_id: UUID
    reference: str
    screenshot_url: Optional[str] = None


class InvoiceResponse(BaseModel):
    id: UUID
    booking_id: Optional[UUID] = None
    amount: int
    currency: str
    status: str
    method: str
    invoice_url: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class CibPaymentRequest(BaseModel):
    booking_id: UUID
    card_number: str
    expiry_month: str
    expiry_year: str
    cvv: str
    holder_name: str
