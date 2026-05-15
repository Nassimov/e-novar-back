from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class TeacherSearchParams(BaseModel):
    query: Optional[str] = None
    wilaya: Optional[str] = None
    subject: Optional[str] = None
    level: Optional[str] = None
    price_min: Optional[int] = None
    price_max: Optional[int] = None
    min_rating: Optional[float] = None
    mode: Optional[str] = None  # online / in-person
    session_type: Optional[str] = None
    page: int = Field(default=1, ge=1)
    size: int = Field(default=20, ge=1, le=100)


class TeacherListItem(BaseModel):
    id: UUID
    user_id: UUID
    full_name: str
    avatar_url: Optional[str] = None
    subjects: List[str] = []
    levels: List[str] = []
    price_per_session: int
    modes: List[str] = []
    rating: float
    reviews_count: int
    badge: Optional[str] = None
    wilaya: Optional[str] = None
    experience_years: int
    is_approved: bool

    model_config = {"from_attributes": True}


class TeacherListResponse(BaseModel):
    items: List[TeacherListItem]
    total: int
    page: int
    size: int
    pages: int


class TeacherDetailResponse(BaseModel):
    id: UUID
    user_id: UUID
    full_name: str
    avatar_url: Optional[str] = None
    bio: Optional[str] = None
    subjects: List[str] = []
    levels: List[str] = []
    price_per_session: int
    modes: List[str] = []
    rating: float
    reviews_count: int
    badge: Optional[str] = None
    wilaya: Optional[str] = None
    experience_years: int
    is_approved: bool
    is_verified: bool
    diplomas: List[dict] = []

    model_config = {"from_attributes": True}


class TeacherProfileUpdate(BaseModel):
    subjects: Optional[List[str]] = None
    levels: Optional[List[str]] = None
    price_per_session: Optional[int] = None
    modes: Optional[List[str]] = None
    bio: Optional[str] = None
    experience_years: Optional[int] = None


class SlotCreate(BaseModel):
    day_of_week: int = Field(ge=0, le=6)
    start_time: str  # "HH:MM"
    end_time: str  # "HH:MM"


class SlotUpdate(BaseModel):
    is_available: Optional[bool] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None


class SlotResponse(BaseModel):
    id: UUID
    day_of_week: int
    start_time: str
    end_time: str
    is_available: bool

    model_config = {"from_attributes": True}


class DiplomaResponse(BaseModel):
    id: str
    name: str
    url: str


class WithdrawalRequest(BaseModel):
    ep_amount: int = Field(gt=0, description="Nombre d'EP à convertir en DZD")
    iban: str
    bank_holder: str


class WithdrawalResponse(BaseModel):
    id: UUID
    ep_amount: int
    dzd_amount: Optional[int] = None
    status: str
    iban: str
    bank_holder: str
    admin_note: Optional[str] = None
    requested_at: datetime
    processed_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class EvaluationCreate(BaseModel):
    student_id: UUID
    subject: str
    rating: int = Field(ge=1, le=5)
    comment: Optional[str] = None
