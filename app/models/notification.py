from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from sqlmodel import Field, SQLModel


class Notification(SQLModel, table=True):
    __tablename__ = "notifications"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="users.id", index=True)
    title: str = Field()
    body: str = Field()
    type: str = Field(default="general")  # booking/payment/message/system/kp/challenge
    is_read: bool = Field(default=False)
    data: Optional[str] = Field(default=None)  # JSON string with extra data
    created_at: datetime = Field(default_factory=datetime.utcnow)
