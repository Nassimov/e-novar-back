from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID, uuid4

from sqlmodel import Field, SQLModel


class ProofType(str, Enum):
    screenshot = "screenshot"
    file = "file"
    text = "text"
    url = "url"


class ChallengeAudience(str, Enum):
    all = "all"
    student = "student"
    teacher = "teacher"
    parent = "parent"


class ChallengeSubmissionStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    declined = "declined"


class ChallengeDef(SQLModel, table=True):
    __tablename__ = "challenge_defs"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    title: str = Field()
    description: str = Field()
    reward_kp: int = Field(default=100)
    proof_type: ProofType = Field(default=ProofType.text)
    audience: ChallengeAudience = Field(default=ChallengeAudience.all)
    is_active: bool = Field(default=True)
    ends_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ChallengeSubmission(SQLModel, table=True):
    __tablename__ = "challenge_submissions"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    challenge_id: UUID = Field(foreign_key="challenge_defs.id", index=True)
    user_id: UUID = Field(foreign_key="users.id", index=True)
    proof_url: Optional[str] = Field(default=None)
    proof_text: Optional[str] = Field(default=None)
    status: ChallengeSubmissionStatus = Field(default=ChallengeSubmissionStatus.pending)
    reason: Optional[str] = Field(default=None)
    submitted_at: datetime = Field(default_factory=datetime.utcnow)
    reviewed_at: Optional[datetime] = Field(default=None)
