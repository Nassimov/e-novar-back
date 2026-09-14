from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel


class SessionValidationStatus(BaseModel):
    session_id: UUID
    booking_id: Optional[UUID] = None
    status: str
    mode: str = "online"  # "online" | "at_home" | "at_student" — see TutoringSession.mode
    can_end_session: bool
    can_validate: bool = False
    scheduled_end_at: Optional[datetime] = None
    teacher_ended_at: Optional[datetime] = None
    student_validated_at: Optional[datetime] = None
    teacher_confirmed_at: Optional[datetime] = None
    validation_deadline_at: Optional[datetime] = None
    dispute_reason: Optional[str] = None
    dispute_comment: Optional[str] = None
    trust_score: Optional[int] = None
    admin_decision: Optional[str] = None
    admin_review_note: Optional[str] = None
    payment_credited_at: Optional[datetime] = None
    gps_consent: bool = False

    # Group lesson info (2026-09-14 redesign) — is_group is False and the
    # rest trivial (1/1) for an individual session; see
    # app/services/session_validation.py's group_validation_stats.
    is_group: bool = False
    group_total: int = 1
    group_validated: int = 0
    group_threshold_percent: int = 0
    group_threshold_met: bool = False
    group_deadline_at: Optional[datetime] = None
    can_group_confirm: bool = False
    can_file_group_report: bool = False
    group_already_reported: bool = False

    model_config = {"from_attributes": True}


class DisputeRequest(BaseModel):
    reason: str
    comment: Optional[str] = None


class GpsSubmitRequest(BaseModel):
    lat: float
    lng: float


class AdminReviewItem(BaseModel):
    id: UUID
    session_id: UUID
    booking_id: Optional[UUID] = None
    student_name: str
    teacher_name: str
    status: str
    trust_score: Optional[int] = None
    trust_score_breakdown: Optional[Dict[str, Any]] = None
    dispute_reason: Optional[str] = None
    dispute_comment: Optional[str] = None
    dispute_attachments: Optional[List[str]] = None
    teacher_ended_at: Optional[datetime] = None
    student_validated_at: Optional[datetime] = None
    teacher_confirmed_at: Optional[datetime] = None
    scheduled_at: Optional[datetime] = None
    amount: Optional[int] = None
    currency: str = "DZD"
    created_at: datetime


class AdminDecisionRequest(BaseModel):
    note: Optional[str] = None


class TrustScoreSettings(BaseModel):
    trust_weight_student_validation: int
    trust_weight_teacher_confirmation: int
    trust_weight_session_completed: int
    trust_weight_online_duration: int
    trust_weight_gps_proximity: int
    trust_weight_clean_history: int
    trust_auto_approve_threshold: int
    trust_manual_review_threshold: int
    room_join_minutes_before: int
    student_validation_window_hours: int
    teacher_confirmation_window_hours: int
    gps_proximity_threshold_meters: int
