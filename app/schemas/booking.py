from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class BookingCreate(BaseModel):
    teacher_id: UUID
    formula: str = Field(default="single")  # single/pack5/monthly
    mode: str = Field(default="online")
    date: Optional[str] = None  # "YYYY-MM-DD"
    slot_time: Optional[str] = None
    duration_minutes: int = Field(default=60)
    payment_method: Optional[str] = None
    promo_code: Optional[str] = None
    subject: Optional[str] = None
    level: Optional[str] = None
    notes: Optional[str] = None


class BookingResponse(BaseModel):
    id: UUID
    student_id: UUID
    teacher_id: UUID
    formula: str
    mode: str
    date: Optional[str] = None
    slot_time: Optional[str] = None
    duration_minutes: int
    status: str
    amount_dzd: int
    kp_reward: int
    promo_code: Optional[str] = None
    payment_method: Optional[str] = None
    payment_status: str
    subject: Optional[str] = None
    level: Optional[str] = None
    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class BookingUpdate(BaseModel):
    status: Optional[str] = None
    date: Optional[str] = None  # "YYYY-MM-DD"
    slot_time: Optional[str] = None
    notes: Optional[str] = None
