from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class Badge(SQLModel, table=True):
    """
    Mirrors public.badges.
    Text PK (slug-like, e.g. 'first-lesson', 'streak-7').
    Static catalogue seeded by migration 025_badge_catalog.sql (student badges)
    and 058_teacher_trophies.sql (teacher trophies). `audience` scopes each
    row to student | teacher | both.
    """

    __tablename__ = "badges"

    id: str = Field(primary_key=True)
    name: str = Field()
    description: Optional[str] = Field(default=None)
    condition: Optional[str] = Field(default=None)
    icon: Optional[str] = Field(default=None)
    tier: str = Field()                                  # bronze | silver | gold | platinum
    category: str = Field()                              # Apprentissage | Régularité | Excellence | Communauté

    # migration 025 — condition engine fields
    condition_type: Optional[str] = Field(default=None)        # sessions_completed | streak_days | …
    condition_threshold: Optional[int] = Field(default=None)   # numeric goal
    condition_subject_slug: Optional[str] = Field(default=None)
    ep_reward: int = Field(default=50)
    sort_order: int = Field(default=0)
    active: bool = Field(default=True)

    # migration 058 — scopes a badge to student / teacher / both audiences
    audience: str = Field(default="student")

    # Phase 14 (Competitive Arena) — migration 094. rarity/is_hidden satisfy
    # spec's "Rarity"/"Hidden Rewards" sections for ANY badge, not just Arena
    # ones (a legacy tutoring/homework badge could be marked rare too).
    # reward_config is the multi-reward-type list beyond the existing single
    # ep_reward int (e.g. a badge can ALSO grant a title/frame) — see
    # reward_service.grant_reward's Phase 14 extension. event_key/
    # available_from/available_until support spec's "Special Events" —
    # temporary, campaign-scoped achievements.
    rarity: str = Field(default="common")
    is_hidden: bool = Field(default=False)
    reward_config: Any = Field(
        default_factory=list,
        sa_column=sa.Column(JSONB, nullable=False, server_default="'[]'::jsonb"),
    )
    event_key: Optional[str] = Field(default=None)
    available_from: Optional[datetime] = Field(default=None)
    available_until: Optional[datetime] = Field(default=None)


class UserBadge(SQLModel, table=True):
    """
    Mirrors public.user_badges.
    UNIQUE on (user_id, badge_id) — a user earns each badge at most once.
    viewed_at IS NULL means the unlock animation has not been shown yet.
    """

    __tablename__ = "user_badges"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    badge_id: str = Field(foreign_key="badges.id", index=True)
    unlocked_at: datetime = Field(default_factory=datetime.utcnow)
    progress_current: int = Field(default=0)
    progress_total: Optional[int] = Field(default=None)
    viewed_at: Optional[datetime] = Field(default=None)  # migration 025 — NULL = unseen animation

    __table_args__ = (
        sa.UniqueConstraint("user_id", "badge_id", name="uq_user_badge"),
    )


class Challenge(SQLModel, table=True):
    """
    Mirrors public.challenges.
    Text PK (slug-like, e.g. 'give_10_sessions').
    audience: student | teacher | both.
    proof_type: image | pdf | image-or-pdf | text | url | any.
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
    timer_duration_sec: Optional[int] = Field(default=None)
    rules: Optional[str] = Field(default=None)
    proof_instructions: Optional[str] = Field(default=None)
    approval_conditions: Optional[str] = Field(default=None)


class ChallengeParticipation(SQLModel, table=True):
    """
    Mirrors public.challenge_participations.
    UNIQUE on (challenge_id, user_id) — a user participates in a challenge once.
    status lifecycle: in_progress → submitted → approved | declined | cancelled | expired | lost
    """

    __tablename__ = "challenge_participations"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    challenge_id: str = Field(foreign_key="challenges.id", index=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    started_at: datetime = Field(default_factory=datetime.utcnow)
    progress: int = Field(default=0)
    total: int = Field(default=1)
    status: str = Field(default="in_progress")           # public.challenge_status
    proof_url: Optional[str] = Field(default=None)       # first file path (backward compat)
    proof_name: Optional[str] = Field(default=None)      # first file original name
    proof_message: Optional[str] = Field(default=None)   # user's optional proof description
    submitted_at: Optional[datetime] = Field(default=None)
    reviewed_by: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    reviewed_at: Optional[datetime] = Field(default=None)
    reason: Optional[str] = Field(default=None)
    deadline_at: Optional[datetime] = Field(default=None)

    __table_args__ = (
        sa.UniqueConstraint("challenge_id", "user_id", name="uq_challenge_participation"),
    )


class ChallengeProofFile(SQLModel, table=True):
    """
    One record per uploaded proof file. A participation may have 1–5 files.
    Files are stored in the private 'challenge-proofs' Supabase bucket.
    storage_path is the path within the bucket; signed URLs are generated on demand.
    """

    __tablename__ = "challenge_proof_files"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    participation_id: UUID = Field(foreign_key="challenge_participations.id", index=True)
    storage_path: str = Field()
    original_filename: str = Field()
    mime_type: str = Field()
    file_size_bytes: int = Field(default=0)
    sort_order: int = Field(default=0)
    uploaded_at: datetime = Field(default_factory=datetime.utcnow)
