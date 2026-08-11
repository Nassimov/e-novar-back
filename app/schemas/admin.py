from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


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
    target_role: str = Field(default="all")
    active: bool = True

    @field_validator("target_role")
    @classmethod
    def validate_target_role(cls, v: str) -> str:
        from app.services.promo_targeting import VALID_TARGET_ROLES
        if v not in VALID_TARGET_ROLES:
            raise ValueError(f"target_role invalide : '{v}'.")
        return v


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

    @field_validator("target_role")
    @classmethod
    def validate_target_role(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        from app.services.promo_targeting import VALID_TARGET_ROLES
        if v not in VALID_TARGET_ROLES:
            raise ValueError(f"target_role invalide : '{v}'.")
        return v
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


class PlatformPricingSettings(BaseModel):
    pack5_discount_percent: int = Field(ge=0, le=100)
    pack10_discount_percent: int = Field(ge=0, le=100)
    # Strictly > 0 (not >= 0 like the packs): a group session must always be
    # cheaper than a single one, by business rule — there is no "0% group
    # discount" option.
    group_discount_percent: int = Field(gt=0, le=100)

    @field_validator("pack10_discount_percent")
    @classmethod
    def validate_pack10_better(cls, v: int, info) -> int:
        pack5 = info.data.get("pack5_discount_percent")
        if pack5 is not None and v <= pack5:
            raise ValueError(
                "La réduction du pack de 10 doit être supérieure à celle du pack de 5."
            )
        return v


class CompetitiveArenaSettings(BaseModel):
    """Phase 7's MMR Calculator knobs (shipped in migration 082, but never
    exposed via an admin endpoint until now — league/season CRUD existed,
    the raw MMR tuning knobs didn't) folded together with Phase 8's
    spectator/prediction/reaction/chat knobs (migration 083) into one
    section, mirroring app/routers/admin/settings.py's existing
    _serialize/GET/PUT pattern (e.g. /pricing)."""

    # Phase 7 — MMR Calculator (app/services/competitive/ranking_service.py)
    competitive_mmr_initial_rating: int = Field(ge=0, le=5000)
    competitive_mmr_k_factor: int = Field(ge=1, le=200)
    competitive_mmr_streak_bonus_per_win: int = Field(ge=0, le=50)
    competitive_mmr_streak_bonus_max: int = Field(ge=0, le=500)
    competitive_mmr_floor: int = Field(ge=0, le=5000)
    competitive_mmr_inflation_dampening_threshold: int = Field(ge=0, le=10000)
    competitive_mmr_inflation_dampening_factor: float = Field(ge=0, le=1)
    competitive_season_default_reset_strategy: str
    competitive_season_default_reset_percentage: int = Field(ge=0, le=100)

    # Phase 8 — Spectator Mode, Live Reactions & Predictions
    competitive_spectator_max_default: Optional[int] = Field(default=None, ge=0)
    competitive_prediction_lock_before_position: int = Field(ge=0, le=100)
    competitive_prediction_reward_arena_xp: int = Field(ge=0, le=1000)
    competitive_prediction_reward_spectator_xp: int = Field(ge=0, le=1000)
    competitive_reaction_rate_limit_per_10s: int = Field(ge=1, le=100)
    competitive_chat_rate_limit_per_10s: int = Field(ge=1, le=100)

    # Phase 9 — Tournament System scheduling knobs
    competitive_tournament_match_start_delay_seconds: int = Field(ge=0, le=3600)
    competitive_tournament_round_advance_grace_minutes: int = Field(ge=0, le=1440)

    # Phase 11 — Clash Club (Club vs Club) config knobs
    competitive_club_creation_cost_ep: int = Field(ge=0, le=100000)
    competitive_club_min_level: Optional[int] = Field(default=None, ge=0, le=100)
    competitive_club_min_rating: Optional[int] = Field(default=None, ge=0, le=5000)
    competitive_club_min_account_age_days: Optional[int] = Field(default=None, ge=0, le=3650)
    competitive_club_default_max_members: int
    competitive_club_name_min_length: int = Field(ge=1, le=100)
    competitive_club_name_max_length: int = Field(ge=1, le=200)

    @field_validator("competitive_club_default_max_members")
    @classmethod
    def _valid_club_default_max_members(cls, v: int) -> int:
        if v not in (20, 50, 100, 250, 500):
            raise ValueError("competitive_club_default_max_members must be one of 20|50|100|250|500")
        return v

    @field_validator("competitive_season_default_reset_strategy")
    @classmethod
    def _valid_reset_strategy(cls, v: str) -> str:
        if v not in ("soft", "hard", "percentage"):
            raise ValueError("competitive_season_default_reset_strategy must be one of soft|hard|percentage")
        return v


class BankTransferSettings(BaseModel):
    bank_beneficiary_name: str = Field(min_length=1, max_length=200)
    platform_rib_cib: Optional[str] = Field(default=None, pattern=r"^\d{20}$")
    platform_rib_edahabia: Optional[str] = Field(default=None, pattern=r"^\d{20}$")
    # How many DZD = 1 EUR — Stripe (international CIB payment) settles in
    # EUR, so this is what converts the DZD-priced booking at checkout time.
    dzd_per_eur: float = Field(gt=0, le=1000)


def _validate_escalating_days(v: List[int]) -> List[int]:
    if any(d <= 0 for d in v):
        raise ValueError("Chaque palier doit être un nombre de jours positif.")
    if any(v[i] > v[i + 1] for i in range(len(v) - 1)):
        raise ValueError("Les paliers de suspension doivent être croissants (ex: 2, 5, 10).")
    return v


class BookingPolicySettings(BaseModel):
    """See docs/migrations/067_booking_safety_rules.sql and
    docs/migrations/069_fairness_pass.sql for the full rule write-up."""
    booking_teacher_response_hours: int = Field(ge=1, le=168)
    booking_refusal_block_threshold: int = Field(ge=1, le=10)
    booking_no_response_suspension_days: List[int] = Field(min_length=1, max_length=10)
    booking_no_response_reset_days: int = Field(ge=1, le=365)
    online_no_show_grace_minutes: int = Field(ge=1, le=120)
    # Student no-show — booking-only suspension (never a full account lock,
    # see app/services/booking_safety.py's apply_student_strike). First
    # offense is always a warning regardless of this list — these paliers
    # only apply from the 2nd incident onward.
    student_no_show_suspension_days: List[int] = Field(min_length=1, max_length=10)
    student_no_show_reset_days: int = Field(ge=1, le=365)
    # In-person (at_home/at_student) absence reports auto-resolve in the
    # filer's favor after this many hours if the other party never counters.
    in_person_dispute_auto_resolve_hours: int = Field(ge=1, le=336)
    # cash/transfer/rib_cib/rib_edahabia auto-cancel if an admin never
    # confirms/rejects the payment within this window — never strikes anyone.
    manual_payment_expiry_hours: int = Field(ge=1, le=336)

    @field_validator("booking_no_response_suspension_days")
    @classmethod
    def validate_escalating(cls, v: List[int]) -> List[int]:
        return _validate_escalating_days(v)

    @field_validator("student_no_show_suspension_days")
    @classmethod
    def validate_student_escalating(cls, v: List[int]) -> List[int]:
        return _validate_escalating_days(v)
