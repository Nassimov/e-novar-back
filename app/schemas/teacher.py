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


class DeliveryOption(BaseModel):
    """One (mode, type) capability a teacher offers — see TeacherDeliveryOption."""
    mode: str   # "online" | "at_student" | "at_home"
    type: str   # "individual" | "group"


class TeacherSubjectItem(BaseModel):
    subject_id: UUID
    subject_name: str
    level_id: UUID
    level_code: str   # round-trip key for updates — see TeacherSubjectUpdateItem
    level_name: str   # display label only
    price_single: int
    price_pack5: int   # computed from price_single + platform discount config, never stored
    price_pack10: int  # computed from price_single + platform discount config, never stored


class TeacherSubjectUpdateItem(BaseModel):
    """Name/code based (not UUIDs) — matches the onboarding contract, so the
    frontend never needs to look up subject/level IDs; the backend upserts by
    name via the same helpers onboarding uses."""
    subject: str
    level: str
    price_single: int = Field(ge=0)


class TeacherDiplomaItem(BaseModel):
    id: UUID
    name: str
    file_url: Optional[str] = None
    file_type: Optional[str] = None
    verified: bool = False


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
    headline: Optional[str] = None
    subjects: List[TeacherSubjectItem] = []
    price_per_session: int
    delivery_options: List[DeliveryOption] = []
    rating: float
    reviews_count: int
    badge: Optional[str] = None
    wilaya: Optional[str] = None
    teaching_wilaya: Optional[str] = None
    teaching_wilayas: List[str] = []
    teaching_nationwide: bool = False
    languages: List[str] = []
    experience_years: int
    success_rate: Optional[float] = None
    students_count: int = 0
    hours_taught: int = 0
    is_approved: bool
    is_verified: bool
    diplomas: List[TeacherDiplomaItem] = []

    model_config = {"from_attributes": True}


class TeacherProfileUpdate(BaseModel):
    headline: Optional[str] = None
    bio: Optional[str] = None
    experience_years: Optional[int] = None
    price_per_session: Optional[int] = None
    teaching_wilaya: Optional[str] = None
    teaching_wilayas: Optional[List[str]] = None
    teaching_nationwide: Optional[bool] = None
    languages: Optional[List[str]] = None
    # Full-replace lists: when provided, they replace the teacher's entire
    # current set. Omit the field entirely to leave it untouched.
    delivery_options: Optional[List[DeliveryOption]] = None
    subjects: Optional[List[TeacherSubjectUpdateItem]] = None


class SlotSubjectLevel(BaseModel):
    subject_id: UUID
    level_id: UUID


class SlotCreate(BaseModel):
    date: str                   # "YYYY-MM-DD"
    start_time: str             # "HH:MM"
    end_time: str               # "HH:MM"
    type: str = "individual"    # "individual" | "group"
    max_students: int = 1
    mode: str = "online"        # "online" | "at_student" | "at_home"
    price: int = 0              # DZD — single session price. Pack5/pack10 are never
                                 # teacher-set; they're always computed from this price
                                 # and the platform's admin-configured discounts.
    status: str = "open"        # "open" | "blocked" | "draft"
    subject_levels: List[SlotSubjectLevel] = Field(min_length=1)


class SlotUpdate(BaseModel):
    date: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    type: Optional[str] = None
    max_students: Optional[int] = None
    mode: Optional[str] = None
    price: Optional[int] = None
    status: Optional[str] = None
    subject_levels: Optional[List[SlotSubjectLevel]] = None  # full-replace when provided


class SlotSubjectLevelResponse(BaseModel):
    subject_id: UUID
    subject_name: str
    level_id: UUID
    level_name: str


class SlotResponse(BaseModel):
    id: str
    date: str
    start_time: str
    end_time: str
    type: str
    max_students: int
    mode: str
    price: int
    price_pack5: int = 0   # computed from `price` + platform discount config, never stored
    price_pack10: int = 0  # computed from `price` + platform discount config, never stored
    status: str
    student_name: Optional[str] = None
    subject_levels: List[SlotSubjectLevelResponse] = []

    model_config = {"from_attributes": True}


class TeacherBookingStudentInfo(BaseModel):
    id: str
    full_name: str
    avatar_url: Optional[str] = None


class TeacherBookingItem(BaseModel):
    id: str
    student: Optional[TeacherBookingStudentInfo] = None
    formula: str
    mode: str
    date: Optional[str] = None
    slot_time: Optional[str] = None
    duration_min: int
    amount: int
    status: str
    stripe_cs_id: Optional[str] = None
    stripe_pi_id: Optional[str] = None
    created_at: str


class WalletResponse(BaseModel):
    wallet_balance_dzd: int
    payout_mode: str
    ep_balance: int
    ep_total_earned: int
    iban: Optional[str] = None
    bank_holder: Optional[str] = None
    bank_last4: Optional[str] = None


class PayoutModeUpdate(BaseModel):
    payout_mode: str  # "platform" | "direct"


class DzdWithdrawalRequest(BaseModel):
    amount_dzd: int = Field(gt=0, description="Montant à retirer en DZD")
    iban: str
    bank_holder: str


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
