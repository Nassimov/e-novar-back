"""Session validation & trust-score workflow (migration 063).

One SessionValidation row per TutoringSession (1:1) — created when a booking
is accepted, alongside its TutoringSession row. Tracks the entire
proof-of-attendance lifecycle independently of `sessions.status` (which stays
the coarse existing enum); see app/services/session_validation.py for the
state machine and app/routers/session_validation.py for the endpoints.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class SessionValidation(SQLModel, table=True):
    __tablename__ = "session_validations"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    session_id: UUID = Field(foreign_key="sessions.id", index=True)
    booking_id: Optional[UUID] = Field(default=None, foreign_key="bookings.id")
    student_id: UUID = Field(foreign_key="profiles.id", index=True)
    teacher_id: UUID = Field(foreign_key="profiles.id", index=True)

    status: str = Field(default="scheduled", index=True)

    token_hash: Optional[str] = Field(default=None)
    token_expires_at: Optional[datetime] = Field(default=None)
    token_shown_at: Optional[datetime] = Field(default=None)
    token_consumed_at: Optional[datetime] = Field(default=None)
    token_method: Optional[str] = Field(default=None)

    teacher_ended_at: Optional[datetime] = Field(default=None)
    student_validated_at: Optional[datetime] = Field(default=None)
    validation_method: Optional[str] = Field(default=None)
    teacher_confirmed_at: Optional[datetime] = Field(default=None)

    dispute_reason: Optional[str] = Field(default=None)
    dispute_comment: Optional[str] = Field(default=None)
    dispute_attachments: Optional[Any] = Field(
        default=None, sa_column=sa.Column(JSONB, nullable=True),
    )
    dispute_created_at: Optional[datetime] = Field(default=None)

    # Structured absence reports (in addition to the free-text dispute_reason
    # above) — 'student_absent' | 'teacher_absent'. See
    # app/routers/session_validation.py's dispute_session and
    # app/workers/booking_tasks.py's task_auto_resolve_disputes: if the other
    # party doesn't counter within in_person_dispute_auto_resolve_hours, it's
    # auto-resolved in the filer's favor.
    dispute_reason_code: Optional[str] = Field(default=None)
    dispute_filed_by: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    dispute_countered_by: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    dispute_countered_reason: Optional[str] = Field(default=None)
    dispute_countered_at: Optional[datetime] = Field(default=None)
    dispute_auto_resolve_at: Optional[datetime] = Field(default=None)

    trust_score: Optional[int] = Field(default=None)
    trust_score_breakdown: Optional[Any] = Field(
        default=None, sa_column=sa.Column(JSONB, nullable=True),
    )

    online_connected_at: Optional[datetime] = Field(default=None)
    online_disconnected_at: Optional[datetime] = Field(default=None)
    online_duration_min: Optional[int] = Field(default=None)

    gps_consent: bool = Field(default=False)
    gps_teacher_lat: Optional[float] = Field(default=None)
    gps_teacher_lng: Optional[float] = Field(default=None)
    gps_teacher_at: Optional[datetime] = Field(default=None)
    gps_student_lat: Optional[float] = Field(default=None)
    gps_student_lng: Optional[float] = Field(default=None)
    gps_student_at: Optional[datetime] = Field(default=None)

    admin_decision: Optional[str] = Field(default=None)
    admin_reviewed_by: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    admin_review_note: Optional[str] = Field(default=None)
    admin_reviewed_at: Optional[datetime] = Field(default=None)

    payment_eligible_at: Optional[datetime] = Field(default=None)
    payment_credited_at: Optional[datetime] = Field(default=None)

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class SessionValidationAuditLog(SQLModel, table=True):
    """Immutable audit trail — insert-only, never updated or deleted."""

    __tablename__ = "session_validation_audit_log"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    session_id: Optional[UUID] = Field(default=None, foreign_key="sessions.id", index=True)
    booking_id: Optional[UUID] = Field(default=None, foreign_key="bookings.id")
    actor_user_id: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    actor_ip: Optional[str] = Field(default=None)
    action: str = Field()
    metadata_: Optional[Any] = Field(
        default=None,
        sa_column=sa.Column("metadata", JSONB, nullable=True),
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)
