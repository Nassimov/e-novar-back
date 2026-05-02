from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class BookingFormula(str, Enum):
    single = "single"
    pack5 = "pack5"
    monthly = "monthly"


class BookingStatus(str, Enum):
    pending = "pending"
    confirmed = "confirmed"
    cancelled = "cancelled"
    completed = "completed"


class PaymentStatus(str, Enum):
    pending = "pending"
    completed = "completed"
    failed = "failed"
    refunded = "refunded"


class Booking(SQLModel, table=True):
    __tablename__ = "bookings"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    student_id: UUID = Field(foreign_key="users.id", index=True)
    teacher_id: UUID = Field(foreign_key="users.id", index=True)
    formula: str = Field(default="single")
    mode: str = Field(default="online")  # online / in-person
    date: Optional[str] = Field(default=None, sa_column=sa.Column(sa.String, nullable=True))  # YYYY-MM-DD
    slot_time: Optional[str] = Field(default=None)  # "HH:MM"
    duration_minutes: int = Field(default=60)
    status: str = Field(default="pending")
    amount_dzd: int = Field(default=0)
    kp_reward: int = Field(default=0)
    promo_code: Optional[str] = Field(default=None)
    payment_method: Optional[str] = Field(default=None)  # cib/edahabia/transfer/cash
    payment_status: str = Field(default="pending")
    subject: Optional[str] = Field(default=None)
    level: Optional[str] = Field(default=None)
    notes: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
