from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from sqlmodel import Field, SQLModel


class TeacherProfile(SQLModel, table=True):
    __tablename__ = "teacher_profiles"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="users.id", unique=True, index=True)
    subjects: Optional[str] = Field(default="[]")  # JSON array
    levels: Optional[str] = Field(default="[]")  # JSON array
    price_per_session: int = Field(default=0)  # DZD
    modes: Optional[str] = Field(default='["online"]')  # JSON array: online/in-person
    bio: Optional[str] = Field(default=None)
    experience_years: int = Field(default=0)
    is_verified: bool = Field(default=False)
    is_approved: bool = Field(default=False)
    rating: float = Field(default=0.0)
    reviews_count: int = Field(default=0)
    badge: Optional[str] = Field(default=None)  # e.g. "top_teacher", "new"
    bank_iban: Optional[str] = Field(default=None)
    bank_holder: Optional[str] = Field(default=None)
    bank_last4: Optional[str] = Field(default=None)
    withdrawal_balance: int = Field(default=0)  # DZD
    diplomas: Optional[str] = Field(default="[]")  # JSON array of {url, name}
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class TeacherSlot(SQLModel, table=True):
    __tablename__ = "teacher_slots"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    teacher_id: UUID = Field(foreign_key="users.id", index=True)
    day_of_week: int = Field(ge=0, le=6)  # 0=Monday, 6=Sunday
    start_time: str = Field()  # "HH:MM"
    end_time: str = Field()  # "HH:MM"
    is_available: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class TeacherWithdrawal(SQLModel, table=True):
    __tablename__ = "teacher_withdrawals"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    teacher_id: UUID = Field(foreign_key="users.id", index=True)
    amount: int = Field()  # DZD
    status: str = Field(default="pending")  # pending/approved/rejected/paid
    iban: Optional[str] = Field(default=None)
    holder: Optional[str] = Field(default=None)
    notes: Optional[str] = Field(default=None)
    processed_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
