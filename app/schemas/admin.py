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
    discount_percent: Optional[int] = Field(default=None, ge=1, le=100)
    discount_dzd: Optional[int] = Field(default=None, gt=0)
    max_uses: int = Field(default=100, gt=0)
    expires_at: Optional[datetime] = None


class PromoCodeUpdate(BaseModel):
    discount_percent: Optional[int] = None
    discount_dzd: Optional[int] = None
    max_uses: Optional[int] = None
    expires_at: Optional[datetime] = None
    is_active: Optional[bool] = None


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
