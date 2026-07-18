from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlmodel import Field, SQLModel


class PlatformSettings(SQLModel, table=True):
    """
    Mirrors public.platform_settings.
    Singleton row (id is always TRUE) holding platform-wide, admin-configurable
    business parameters. Currently: pack5/pack10/group booking discount
    percentages — the only place these percentages are ever defined; see
    app/services/pricing.py.
    """

    __tablename__ = "platform_settings"

    id: bool = Field(default=True, primary_key=True)
    pack5_discount_percent: int = Field(default=10)
    pack10_discount_percent: int = Field(default=15)
    group_discount_percent: int = Field(default=20)

    # Session validation trust score (see app/services/session_validation.py)
    # — weights need not sum to 100, the engine normalizes by whatever total
    # is actually configured. Never hardcode these values elsewhere.
    trust_weight_student_validation: int = Field(default=40)
    trust_weight_teacher_confirmation: int = Field(default=20)
    trust_weight_session_completed: int = Field(default=10)
    trust_weight_online_duration: int = Field(default=20)
    trust_weight_gps_proximity: int = Field(default=10)
    trust_weight_clean_history: int = Field(default=10)
    trust_auto_approve_threshold: int = Field(default=80)
    trust_manual_review_threshold: int = Field(default=50)
    token_visible_minutes_before: int = Field(default=30)
    student_validation_window_hours: int = Field(default=24)
    gps_proximity_threshold_meters: int = Field(default=500)

    # Booking safety rules (see app/routers/student_teachers.py +
    # app/workers/booking_tasks.py). A teacher has this many hours to
    # accept/refuse a paid booking before it's auto-cancelled (student not
    # charged). Consecutive no-responses escalate a self-expiring suspension
    # (one entry of booking_no_response_suspension_days per consecutive
    # strike, last value repeats past the end of the list); the strike
    # counter resets after booking_no_response_reset_days of clean history.
    # Separately, booking_refusal_block_threshold refused/no-response
    # attempts on the exact same (teacher, subject, weekday, time) combo
    # permanently blocks the student from re-booking that exact combo.
    booking_teacher_response_hours: int = Field(default=24)
    booking_refusal_block_threshold: int = Field(default=2)
    booking_no_response_suspension_days: List[int] = Field(
        default=[2, 5, 10],
        sa_column=sa.Column(ARRAY(sa.Integer), nullable=False, server_default="'{2,5,10}'"),
    )
    booking_no_response_reset_days: int = Field(default=60)

    # Online-session no-show detection (see app/workers/booking_tasks.py's
    # task_detect_online_teacher_no_show). If the teacher hasn't joined the
    # LiveKit room within this many minutes of the scheduled start, the
    # session is marked no_show, the student is refunded 100%, and the
    # teacher gets a strike via the same escalating suspension counter above.
    online_no_show_grace_minutes: int = Field(default=20)

    # Platform's own receiving accounts for the manual RIB CIB / RIB Edahabia
    # bank-transfer payment methods (see docs/migrations/068_rib_payment_methods.sql
    # and app/routers/public.py's bank-transfer-info endpoint). A student
    # paying via one of these rails transfers into whichever of these RIBs is
    # set here — real automated debit isn't built yet, this phase is manual/
    # admin-reconciled only, same as the existing cash/transfer flow.
    bank_beneficiary_name: str = Field(default="E-NOVAR SARL")
    platform_rib_cib: Optional[str] = Field(default=None)
    platform_rib_edahabia: Optional[str] = Field(default=None)

    # Student no-show enforcement — mirrors the teacher escalation below but
    # only ever blocks new bookings (see StudentProfile.booking_suspended_until).
    student_no_show_suspension_days: List[int] = Field(
        default=[3, 7, 14],
        sa_column=sa.Column(ARRAY(sa.Integer), nullable=False, server_default="'{3,7,14}'"),
    )
    student_no_show_reset_days: int = Field(default=60)

    # In-person (at_home/at_student) absence reports (see
    # app/routers/session_validation.py's dispute_reason_code and
    # app/workers/booking_tasks.py's task_auto_resolve_disputes) — auto-
    # resolved in the filer's favor if the other party never counters.
    in_person_dispute_auto_resolve_hours: int = Field(default=48)

    updated_at: datetime = Field(default_factory=datetime.utcnow)


class CmsPage(SQLModel, table=True):
    """
    Mirrors public.cms_pages.
    Admin-editable static pages (terms, privacy, help, cookies).
    slug is the unique identifier (e.g. 'terms-of-service').
    """

    __tablename__ = "cms_pages"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    slug: str = Field(sa_column=sa.Column(sa.String, unique=True, nullable=False))
    title: str = Field()
    body_md: Optional[str] = Field(default=None)
    version: int = Field(default=1)
    published_at: Optional[datetime] = Field(default=None)


class LegalAcceptance(SQLModel, table=True):
    """
    Mirrors public.legal_acceptances.
    UNIQUE on (user_id, page_slug, version) — tracks which legal version a user accepted.
    """

    __tablename__ = "legal_acceptances"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    page_slug: str = Field()
    version: int = Field()
    accepted_at: datetime = Field(default_factory=datetime.utcnow)

    __table_args__ = (
        sa.UniqueConstraint("user_id", "page_slug", "version", name="uq_legal_acceptance"),
    )


class PromoCode(SQLModel, table=True):
    """
    Mirrors public.promo_codes.

    Two mutually exclusive reward modes:
    - KP bonus  (kp_reward > 0)          : awarded immediately when user applies the code.
    - Discount  (discount_type is set)    : applied at booking checkout to reduce the price.
    A code may have both (e.g. +50 EP bonus + -10% discount).

    valid_from / valid_to : optional activation window.
    max_uses              : None = unlimited global uses.
    target_role           : 'all' | 'student' | 'teacher' | 'parent'
    """

    __tablename__ = "promo_codes"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    code: str = Field(sa_column=sa.Column(sa.String, unique=True, nullable=False))
    title: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None)
    # Booking-discount reward
    discount_type: Optional[str] = Field(
        default=None,
        sa_column=sa.Column(sa.String, nullable=True),
    )  # 'percent' | 'fixed' | None
    discount_value: int = Field(default=0)
    # Instant KP reward
    kp_reward: int = Field(default=0)
    # Validity
    valid_from: Optional[datetime] = Field(default=None)
    valid_to: Optional[datetime] = Field(default=None)
    max_uses: Optional[int] = Field(default=None)
    uses: int = Field(default=0)
    active: bool = Field(default=True)
    target_role: str = Field(default="all")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class PromoRedemption(SQLModel, table=True):
    """
    Mirrors public.promo_redemptions.
    UNIQUE on (code_id, user_id) — a user can redeem a code only once.
    """

    __tablename__ = "promo_redemptions"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    code_id: UUID = Field(foreign_key="promo_codes.id", index=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    booking_id: Optional[UUID] = Field(default=None, foreign_key="bookings.id")
    redeemed_at: datetime = Field(default_factory=datetime.utcnow)

    __table_args__ = (
        sa.UniqueConstraint("code_id", "user_id", name="uq_promo_redemption"),
    )


class AuditLog(SQLModel, table=True):
    """
    Mirrors public.audit_logs.
    Immutable record of admin/system actions.
    meta: jsonb — action-specific payload.
    """

    __tablename__ = "audit_logs"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    actor_id: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    action: str = Field()
    target_type: Optional[str] = Field(default=None)
    target_id: Optional[UUID] = Field(default=None)
    meta: Optional[Any] = Field(
        default=None,
        sa_column=sa.Column(JSONB, nullable=True, server_default="'{}'::jsonb"),
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Report(SQLModel, table=True):
    """
    Mirrors public.reports.
    Moderation reports submitted by users against any entity.
    target_type: 'review' | 'user' | 'teacher' | 'message' | 'challenge'
    """

    __tablename__ = "reports"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    reporter_id: UUID = Field(foreign_key="profiles.id", index=True)
    target_type: str = Field()
    target_id: UUID = Field()
    reason: Optional[str] = Field(default=None)
    status: str = Field(default="open")                  # public.report_status
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Invitation(SQLModel, table=True):
    """
    Mirrors public.invitations.
    Direct email invitations (distinct from referral codes).
    """

    __tablename__ = "invitations"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    inviter_id: UUID = Field(foreign_key="profiles.id", index=True)
    email: str = Field()
    message: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
