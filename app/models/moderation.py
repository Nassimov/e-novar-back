from __future__ import annotations

"""
Sanctions & Appeals — Competitive Arena Phase 16 (migration 096).

No general sanction mechanism existed anywhere on the platform before this
— arena_sanctions is genuinely new. Issuing one reuses whatever real
enforcement already exists (competitive_statistics.suspended_until for
suspensions, competitive_chat_mutes for mutes, club_members.status='banned'
for club restrictions — see app/services/competitive/moderation_service.py)
rather than inventing parallel enforcement; 'warning' and 'tournament_ban'
have no prior mechanism, so this table IS their enforcement record.
"""

from datetime import datetime
from typing import Any, Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

SANCTION_TYPES = [
    "warning", "mute_temporary", "mute_permanent", "suspension_temporary",
    "suspension_permanent", "competitive_ban", "tournament_ban", "club_restriction",
]
SANCTION_STATUSES = ["active", "expired", "revoked"]
APPEAL_STATUSES = ["pending", "accepted", "rejected", "more_info_requested"]


class ArenaSanction(SQLModel, table=True):
    """Mirrors public.arena_sanctions — the unified moderation-action
    record for every Arena sanction, whatever its real enforcement
    mechanism (see module docstring)."""

    __tablename__ = "arena_sanctions"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    sanction_type: str = Field()
    reason: Optional[str] = Field(default=None)
    evidence: Any = Field(default_factory=list, sa_column=sa.Column(JSONB, nullable=False, server_default="'[]'::jsonb"))
    issued_by: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    report_id: Optional[UUID] = Field(default=None, foreign_key="reports.id")
    club_id: Optional[UUID] = Field(default=None, foreign_key="clubs.id")
    starts_at: datetime = Field(default_factory=datetime.utcnow)
    ends_at: Optional[datetime] = Field(default=None)
    status: str = Field(default="active")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ArenaAppeal(SQLModel, table=True):
    """Mirrors public.arena_appeals — UNIQUE(sanction_id): one appeal per
    sanction (a rejected appeal is final; a new sanction gets its own)."""

    __tablename__ = "arena_appeals"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    sanction_id: UUID = Field(foreign_key="arena_sanctions.id", index=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    message: str = Field()
    status: str = Field(default="pending")
    reviewed_by: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    reviewed_at: Optional[datetime] = Field(default=None)
    resolution_message: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    __table_args__ = (
        sa.UniqueConstraint("sanction_id", name="uq_arena_appeals_sanction"),
    )
