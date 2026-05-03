from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class Badge(SQLModel, table=True):
    """
    Mirrors public.badges.
    Text PK (slug-like, e.g. 'first_session', 'streak_7').
    Static catalogue managed by admins.
    """

    __tablename__ = "badges"

    id: str = Field(primary_key=True)
    name: str = Field()
    description: Optional[str] = Field(default=None)
    condition: Optional[str] = Field(default=None)
    icon: Optional[str] = Field(default=None)
    tier: str = Field()                                  # public.badge_tier
    category: str = Field()                              # public.badge_category


class UserBadge(SQLModel, table=True):
    """
    Mirrors public.user_badges.
    UNIQUE on (user_id, badge_id) — a user earns each badge at most once.
    progress_current / progress_total track incremental badge progress.
    """

    __tablename__ = "user_badges"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    badge_id: str = Field(foreign_key="badges.id", index=True)
    unlocked_at: datetime = Field(default_factory=datetime.utcnow)
    progress_current: int = Field(default=0)
    progress_total: Optional[int] = Field(default=None)

    __table_args__ = (
        sa.UniqueConstraint("user_id", "badge_id", name="uq_user_badge"),
    )


class Challenge(SQLModel, table=True):
    """
    Mirrors public.challenges.
    Text PK (slug-like, e.g. 'give_10_sessions').
    audience: student | teacher | both.
    proof_type: image | pdf | image-or-pdf.
    """

    __tablename__ = "challenges"

    id: str = Field(primary_key=True)
    title: str = Field()
    description: Optional[str] = Field(default=None)
    reward: int = Field(default=0)                       # KP reward on completion
    badge_id: Optional[str] = Field(default=None, foreign_key="badges.id")
    audience: str = Field(default="student")             # public.challenge_audience
    proof_type: Optional[str] = Field(default="image-or-pdf")  # public.proof_type
    active: bool = Field(default=True)
    starts_at: Optional[datetime] = Field(default=None)
    ends_at: Optional[datetime] = Field(default=None)


class ChallengeParticipation(SQLModel, table=True):
    """
    Mirrors public.challenge_participations.
    UNIQUE on (challenge_id, user_id) — a user participates in a challenge once.
    status lifecycle: in_progress → submitted → approved | declined | cancelled
    """

    __tablename__ = "challenge_participations"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    challenge_id: str = Field(foreign_key="challenges.id", index=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    started_at: datetime = Field(default_factory=datetime.utcnow)
    progress: int = Field(default=0)
    total: int = Field(default=1)
    status: str = Field(default="in_progress")           # public.challenge_status
    proof_url: Optional[str] = Field(default=None)
    proof_name: Optional[str] = Field(default=None)
    submitted_at: Optional[datetime] = Field(default=None)
    reviewed_by: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    reviewed_at: Optional[datetime] = Field(default=None)
    reason: Optional[str] = Field(default=None)

    __table_args__ = (
        sa.UniqueConstraint("challenge_id", "user_id", name="uq_challenge_participation"),
    )
