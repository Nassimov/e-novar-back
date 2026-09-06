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
    can_view_token: bool
    token_visible_at: Optional[datetime] = None
    scheduled_end_at: Optional[datetime] = None
    teacher_ended_at: Optional[datetime] = None
    student_validated_at: Optional[datetime] = None
    validation_method: Optional[str] = None
    teacher_confirmed_at: Optional[datetime] = None
    validation_deadline_at: Optional[datetime] = None
    dispute_reason: Optional[str] = None
    dispute_comment: Optional[str] = None
    trust_score: Optional[int] = None
    admin_decision: Optional[str] = None
    admin_review_note: Optional[str] = None
    payment_credited_at: Optional[datetime] = None
    gps_consent: bool = False

    model_config = {"from_attributes": True}


class TokenViewResponse(BaseModel):
    token: str
    expires_at: datetime
    already_consumed: bool = False


class ValidateSessionRequest(BaseModel):
    token: str
    method: str  # 'auto_send' | 'manual_entry'


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
    token_visible_minutes_before: int
    student_validation_window_hours: int
    teacher_confirmation_window_hours: int
    gps_proximity_threshold_meters: int
