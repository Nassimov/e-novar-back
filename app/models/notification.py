from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class Notification(SQLModel, table=True):
    """
    Mirrors public.notifications.
    type: booking_confirmed | session_reminder | homework_graded | kp_earned | message | system
    data: jsonb — extra context for deep-linking (e.g. booking_id, session_id).
    channel: in_app | push | email | sms
    read_at = None → unread; set when user opens it.
    """

    __tablename__ = "notifications"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    type: str = Field()
    title: Optional[str] = Field(default=None)
    body: Optional[str] = Field(default=None)
    data: Optional[Any] = Field(
        default=None,
        sa_column=sa.Column(JSONB, nullable=True, server_default="'{}'::jsonb"),
    )
    channel: str = Field(default="in_app")               # public.notif_channel
    read_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class NotificationPreference(SQLModel, table=True):
    """
    Mirrors public.notification_preferences.
    PK = user_id — one preference row per user.
    Created automatically by the handle_new_user() trigger in Supabase.
    """

    __tablename__ = "notification_preferences"

    user_id: UUID = Field(primary_key=True, foreign_key="profiles.id")
    push: bool = Field(default=True)
    email: bool = Field(default=True)
    sms: bool = Field(default=False)
    reminders: bool = Field(default=True)
    sessions: bool = Field(default=True)
    kp: bool = Field(default=True)
    rewards: bool = Field(default=True)
    messages: bool = Field(default=True)
