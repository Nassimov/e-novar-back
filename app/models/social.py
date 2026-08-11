from __future__ import annotations

"""
Generic cross-feature social primitives — not owned by any single feature.

UserBlock is introduced by Competitive Arena Phase 2 (a blocked student can't
challenge you or see your private matches) but is deliberately generic
(profiles <-> profiles, no competitive-specific columns) so chat or any
other feature can reuse it later without a redesign.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlmodel import Field, SQLModel


class UserBlock(SQLModel, table=True):
    """Mirrors public.user_blocks. blocker_id blocked blocked_id."""

    __tablename__ = "user_blocks"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    blocker_id: UUID = Field(foreign_key="profiles.id", index=True)
    blocked_id: UUID = Field(foreign_key="profiles.id", index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
