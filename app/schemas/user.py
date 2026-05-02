from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr


class UserResponse(BaseModel):
    id: UUID
    email: str
    full_name: str
    role: str
    avatar_url: Optional[str] = None
    wilaya: Optional[str] = None
    phone: Optional[str] = None
    bio: Optional[str] = None
    is_active: bool
    is_verified: bool
    onboarding_completed: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ProfileUpdateRequest(BaseModel):
    full_name: Optional[str] = None
    wilaya: Optional[str] = None
    phone: Optional[str] = None
    bio: Optional[str] = None
    avatar_url: Optional[str] = None


class NotificationPrefsUpdate(BaseModel):
    booking_reminders: bool = True
    new_messages: bool = True
    kp_updates: bool = True
    promotions: bool = False
    push_enabled: bool = True
    email_enabled: bool = True


class PaymentMethodCreate(BaseModel):
    type: str  # cib / edahabia / transfer
    holder_name: str
    card_last4: Optional[str] = None
    iban: Optional[str] = None
    bank_name: Optional[str] = None


class PaymentMethodResponse(BaseModel):
    id: str
    type: str
    holder_name: str
    card_last4: Optional[str] = None
    bank_name: Optional[str] = None
    is_default: bool = False
