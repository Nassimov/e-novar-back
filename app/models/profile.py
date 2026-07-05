from __future__ import annotations

from datetime import date, datetime
from typing import Any, List, Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlmodel import Field, SQLModel


class Profile(SQLModel, table=True):
    """
    Mirrors public.profiles.
    id = Supabase auth.users.id — never auto-generated, always provided from JWT sub.
    full_name is a GENERATED ALWAYS AS column in PostgreSQL — read-only here.
    """

    __tablename__ = "profiles"

    id: UUID = Field(primary_key=True)
    first_name: Optional[str] = Field(default=None)
    last_name: Optional[str] = Field(default=None)
    # GENERATED ALWAYS AS (coalesce(first_name,'') || ' ' || coalesce(last_name,'')) STORED
    # sa.Computed tells SQLAlchemy never to include this column in INSERT/UPDATE.
    full_name: Optional[str] = Field(
        default=None,
        sa_column=sa.Column(
            sa.Text,
            sa.Computed(
                "coalesce(first_name,'') || ' ' || coalesce(last_name,'')",
                persisted=True,
            ),
            nullable=True,
        ),
    )
    birth_date: Optional[date] = Field(default=None)
    gender: Optional[str] = Field(default=None)          # public.gender enum value
    email: Optional[str] = Field(
        default=None,
        sa_column=sa.Column(sa.String, unique=True, nullable=True),
    )
    phone: Optional[str] = Field(default=None)
    avatar_url: Optional[str] = Field(default=None)
    active_sticker_url: Optional[str] = Field(default=None)
    bio: Optional[str] = Field(default=None)
    wilaya: Optional[str] = Field(default=None)
    city: Optional[str] = Field(default=None)
    school: Optional[str] = Field(default=None)
    language: Optional[str] = Field(default="fr")        # public.lang enum value
    theme: Optional[str] = Field(default="system")       # public.theme_pref enum value
    onboarding_completed: bool = Field(default=False)
    last_seen_at: Optional[datetime] = Field(default=None)
    referral_code: Optional[str] = Field(
        default=None,
        sa_column=sa.Column(sa.String(30), unique=True, nullable=True),
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class UserRole(SQLModel, table=True):
    """Mirrors public.user_roles — a user may hold multiple roles."""

    __tablename__ = "user_roles"

    id: UUID = Field(default_factory=lambda: __import__("uuid").uuid4(), primary_key=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    role: str = Field()                                   # public.app_role enum value

    __table_args__ = (
        sa.UniqueConstraint("user_id", "role", name="uq_user_role"),
    )


class StudentProfile(SQLModel, table=True):
    """Mirrors public.student_profiles — one row per student user."""

    __tablename__ = "student_profiles"

    user_id: UUID = Field(primary_key=True, foreign_key="profiles.id")
    student_code: Optional[str] = Field(
        default=None,
        sa_column=sa.Column(sa.String, unique=True, nullable=True),
    )
    level_main: Optional[str] = Field(default=None)      # public.level_main
    level_detail: Optional[str] = Field(default=None)
    speciality: Optional[str] = Field(default=None)
    monitoring_mode: Optional[str] = Field(default="solo")  # public.monitoring_mode
    parent_link_code: Optional[str] = Field(
        default=None,
        sa_column=sa.Column(sa.String, unique=True, nullable=True),
    )
    budget_tier: Optional[str] = Field(default=None)     # public.budget_tier
    budget_min: Optional[int] = Field(default=None)
    budget_max: Optional[int] = Field(default=None)
    goals: Optional[List[str]] = Field(
        default=None,
        sa_column=sa.Column(ARRAY(sa.Text), nullable=True, server_default="{}"),
    )
    subjects_interested: Optional[List[str]] = Field(
        default=None,
        sa_column=sa.Column(ARRAY(sa.Text), nullable=True, server_default="{}"),
    )
    online_only: bool = Field(default=False)
    available_days: Optional[List[int]] = Field(
        default=None,
        sa_column=sa.Column(ARRAY(sa.Integer), nullable=True, server_default="{}"),
    )
    available_slots: Optional[List[str]] = Field(
        default=None,
        sa_column=sa.Column(ARRAY(sa.Text), nullable=True, server_default="{}"),
    )
    lesson_format: Optional[str] = Field(default=None)  # "individual" | "group" | "both"
    created_at: datetime = Field(default_factory=datetime.utcnow)


class TeacherProfile(SQLModel, table=True):
    """Mirrors public.teacher_profiles — one row per teacher user."""

    __tablename__ = "teacher_profiles"

    user_id: UUID = Field(primary_key=True, foreign_key="profiles.id")
    headline: Optional[str] = Field(default=None)
    bio_long: Optional[str] = Field(default=None)
    experience_years: int = Field(default=0)
    price_per_session: int = Field(default=0)             # DZD
    verified: bool = Field(default=False)
    badge: Optional[str] = Field(default=None)            # public.teacher_badge
    sponsored: bool = Field(default=False)
    success_rate: Optional[float] = Field(default=None)
    students_count: int = Field(default=0)
    hours_taught: int = Field(default=0)
    rating_avg: float = Field(default=0.0)
    reviews_count: int = Field(default=0)
    kp_reward: int = Field(default=50)
    iban: Optional[str] = Field(default=None)
    bank_holder: Optional[str] = Field(default=None)
    bank_last4: Optional[str] = Field(default=None)
    status: str = Field(default="pending")                # public.teacher_status
    teaching_wilaya: Optional[str] = Field(default=None)
    teaching_wilayas: Optional[List[str]] = Field(
        default=None,
        sa_column=sa.Column(ARRAY(sa.Text), nullable=True, server_default="'{}'"),
    )
    teaching_nationwide: bool = Field(default=False)
    languages: Optional[List[str]] = Field(
        default=None,
        sa_column=sa.Column(ARRAY(sa.Text), nullable=True, server_default="'{}'"),
    )
    cv_url: Optional[str] = Field(default=None)
    cover_letter: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ParentProfile(SQLModel, table=True):
    """Mirrors public.parent_profiles."""

    __tablename__ = "parent_profiles"

    user_id: UUID = Field(primary_key=True, foreign_key="profiles.id")
    relationship: Optional[str] = Field(default=None)
    # Unique code (KRN-P-XXXX) shown to the parent, used by students to link.
    parent_code: Optional[str] = Field(
        default=None,
        sa_column=sa.Column(sa.String, unique=True, nullable=True),
    )
    budget_tier: Optional[str] = Field(default=None)
    budget_min: Optional[int] = Field(default=None)
    budget_max: Optional[int] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AdminProfile(SQLModel, table=True):
    """Mirrors public.admin_profiles."""

    __tablename__ = "admin_profiles"

    user_id: UUID = Field(primary_key=True, foreign_key="profiles.id")
    permissions: Optional[Any] = Field(
        default=None,
        sa_column=sa.Column(JSONB, nullable=True, server_default="{}"),
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)
