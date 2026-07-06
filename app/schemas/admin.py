from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class UserListParams(BaseModel):
    page: int = Field(default=1, ge=1)
    size: int = Field(default=20, ge=1, le=100)
    role: Optional[str] = None
    is_active: Optional[bool] = None
    search: Optional[str] = None


class UserStatusUpdate(BaseModel):
    is_active: bool
    reason: Optional[str] = None


class TeacherApprovalAction(BaseModel):
    action: str  # "approve" or "reject"
    reason: Optional[str] = None


class ReviewModerationAction(BaseModel):
    action: str  # "flag" or "unflag" or "delete"
    reason: Optional[str] = None


class PromoCodeCreate(BaseModel):
    code: str = Field(min_length=3, max_length=30)
    title: Optional[str] = Field(default=None, max_length=80)
    description: Optional[str] = Field(default=None, max_length=300)
    # Instant KP reward (0 = none)
    kp_reward: int = Field(default=0, ge=0)
    # Booking discount (optional)
    discount_type: Optional[str] = None        # 'percent' | 'fixed' | None
    discount_value: int = Field(default=0, ge=0)
    # Validity
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    max_uses: Optional[int] = Field(default=None, gt=0)
    # Targeting
    target_role: str = Field(default="all")    # 'all' | 'student' | 'teacher' | 'parent'
    active: bool = True


class PromoCodeUpdate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=80)
    description: Optional[str] = Field(default=None, max_length=300)
    kp_reward: Optional[int] = Field(default=None, ge=0)
    discount_type: Optional[str] = None
    discount_value: Optional[int] = Field(default=None, ge=0)
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    max_uses: Optional[int] = Field(default=None, gt=0)
    target_role: Optional[str] = None
    active: Optional[bool] = None


class StatsResponse(BaseModel):
    total_users: int
    total_teachers: int
    total_students: int
    total_bookings: int
    total_sessions_completed: int
    total_revenue_dzd: int
    active_users_this_week: int
    new_users_this_month: int
    pending_withdrawals: int
    pending_teacher_approvals: int


class WithdrawalProcessRequest(BaseModel):
    action: str                       # "approve" or "reject"
    dzd_amount: Optional[int] = None  # obligatoire si action == "approve"
    admin_note: Optional[str] = None
