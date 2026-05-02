from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel


class NotificationResponse(BaseModel):
    id: UUID
    user_id: UUID
    title: str
    body: str
    type: str
    is_read: bool
    data: Optional[dict] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class NotificationPrefsUpdate(BaseModel):
    booking_reminders: bool = True
    new_messages: bool = True
    kp_updates: bool = True
    promotions: bool = False
    push_enabled: bool = True
    email_enabled: bool = True
