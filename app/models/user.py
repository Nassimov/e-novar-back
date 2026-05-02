from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID, uuid4

from sqlmodel import Field, SQLModel


class UserRole(str, Enum):
    student = "student"
    teacher = "teacher"
    parent = "parent"
    admin = "admin"


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    supabase_id: str = Field(unique=True, index=True)
    email: str = Field(unique=True, index=True)
    full_name: str = Field(default="")
    role: UserRole = Field(default=UserRole.student)
    avatar_url: Optional[str] = Field(default=None)
    wilaya: Optional[str] = Field(default=None)
    phone: Optional[str] = Field(default=None)
    bio: Optional[str] = Field(default=None)
    is_active: bool = Field(default=True)
    is_verified: bool = Field(default=False)
    onboarding_completed: bool = Field(default=False)
    notification_prefs: Optional[str] = Field(default=None)  # JSON string
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
