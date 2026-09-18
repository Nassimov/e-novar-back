from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from sqlmodel import Field, SQLModel


class LoginEvent(SQLModel, table=True):
    """
    Mirrors public.login_events — one row per sign-in attempt (success or
    failure), powering both the "recent devices" list and the login-history
    audit trail on the account settings page. See app/routers/account_security.py.
    """

    __tablename__ = "login_events"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    success: bool = Field()
    ip: Optional[str] = Field(default=None)
    user_agent: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
