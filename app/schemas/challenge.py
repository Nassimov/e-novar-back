from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel


class ChallengeSubmitRequest(BaseModel):
    proof_url: Optional[str] = None
    proof_text: Optional[str] = None


class ChallengeSubmissionResponse(BaseModel):
    id: UUID
    challenge_id: UUID
    user_id: UUID
    proof_url: Optional[str] = None
    proof_text: Optional[str] = None
    status: str
    reason: Optional[str] = None
    submitted_at: datetime
    reviewed_at: Optional[datetime] = None

    model_config = {"from_attributes": True}
