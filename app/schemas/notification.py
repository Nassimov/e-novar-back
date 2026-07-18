from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, computed_field


class NotificationResponse(BaseModel):
    id: UUID
    user_id: UUID
    title: Optional[str] = None
    body: Optional[str] = None
    type: str
    category: str = "system"
    priority: str = "normal"
    deep_link: Optional[str] = None
    channel: str = "in_app"
    read_at: Optional[datetime] = None
    archived_at: Optional[datetime] = None
    data: Optional[dict] = None
    created_at: datetime

    @computed_field
    @property
    def is_read(self) -> bool:
        return self.read_at is not None

    @computed_field
    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    model_config = {"from_attributes": True}


class NotificationPrefsUpdate(BaseModel):
    push: bool = True
    email: bool = True
    sms: bool = False
    reminders: bool = True
    sessions: bool = True
    kp: bool = True
    rewards: bool = True
    messages: bool = True


class CategoryPrefToggle(BaseModel):
    category: str
    channel: str  # push | email | in_app
    enabled: bool
