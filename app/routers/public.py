"""
Public (unauthenticated) routes — platform stats and leaderboard.
These endpoints are rate-limited via the API gateway but require no token.
"""
from __future__ import annotations

from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, distinct
from sqlmodel import Session, select

from app.dependencies import get_db
from app.models.kp import KpBalance
from app.models.profile import Profile, TeacherProfile, StudentProfile, UserRole
from app.services.pricing import get_platform_settings

router = APIRouter()


class PricingSettingsPublic(BaseModel):
    pack5_discount_percent: int
    pack10_discount_percent: int
    group_discount_percent: int


@router.get("/pricing", response_model=PricingSettingsPublic)
async def get_public_pricing(db: Session = Depends(get_db)):
    """Current pack/group discount percentages — used to preview prices pre-auth."""
    settings = get_platform_settings(db)
    return PricingSettingsPublic(
        pack5_discount_percent=settings.pack5_discount_percent,
        pack10_discount_percent=settings.pack10_discount_percent,
        group_discount_percent=settings.group_discount_percent,
    )


class BookingPolicyPublic(BaseModel):
    booking_teacher_response_hours: int
    in_person_dispute_auto_resolve_hours: int
    manual_payment_expiry_hours: int


@router.get("/booking-policy", response_model=BookingPolicyPublic)
async def get_public_booking_policy(db: Session = Depends(get_db)):
    """Timing rules shown as countdowns to the user (teacher's pending
    request auto-cancel deadline, student's pending-validation window) —
    admin-configurable in app/routers/admin/settings.py."""
    settings = get_platform_settings(db)
    return BookingPolicyPublic(
        booking_teacher_response_hours=settings.booking_teacher_response_hours,
        in_person_dispute_auto_resolve_hours=settings.in_person_dispute_auto_resolve_hours,
        manual_payment_expiry_hours=settings.manual_payment_expiry_hours,
    )


class BankTransferInfoPublic(BaseModel):
    beneficiary_name: str
    rib_cib: Optional[str] = None
    rib_edahabia: Optional[str] = None
    # How many DZD = 1 EUR — lets the frontend show "≈ X EUR" next to the
    # international card payment option before checkout (Stripe settles in
    # EUR, see app/services/stripe.py's create_checkout_session).
    dzd_per_eur: float


@router.get("/bank-transfer-info", response_model=BankTransferInfoPublic)
async def get_bank_transfer_info(db: Session = Depends(get_db)):
    """Platform's own receiving RIBs, shown to a student who picks the RIB
    CIB / RIB Edahabia payment method at booking (see student.payment.tsx) —
    admin-configurable in app/routers/admin/settings.py."""
    settings = get_platform_settings(db)
    return BankTransferInfoPublic(
        beneficiary_name=settings.bank_beneficiary_name,
        rib_cib=settings.platform_rib_cib,
        rib_edahabia=settings.platform_rib_edahabia,
        dzd_per_eur=settings.dzd_per_eur,
    )


# ─── Response schemas ─────────────────────────────────────────────────────────

class PlatformStats(BaseModel):
    verified_teachers: int
    wilayas_covered: int
    active_students: int


class TeacherLeaderEntry(BaseModel):
    id: str
    full_name: str
    avatar_url: Optional[str] = None
    slug: Optional[str] = None
    rating_avg: float
    reviews_count: int
    wallet_balance_dzd: int
    price_per_session: int
    kp_balance: int
    badge: Optional[str] = None
    badge_type: str  # "earnings" | "rating"


class StudentLeaderEntry(BaseModel):
    id: str
    full_name: str
    avatar_url: Optional[str] = None
    kp_balance: int
    kp_week_earned: int
    level: int
    wilaya: Optional[str] = None


class LeaderboardResponse(BaseModel):
    top_by_earnings: List[TeacherLeaderEntry]
    top_by_rating: List[TeacherLeaderEntry]
    top_students: List[StudentLeaderEntry]
    featured_teacher: Optional[TeacherLeaderEntry] = None


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _build_teacher_entry(tp: TeacherProfile, p: Profile, kb: Optional[KpBalance], badge_type: str) -> TeacherLeaderEntry:
    return TeacherLeaderEntry(
        id=str(tp.user_id),
        full_name=p.full_name or f"{p.first_name or ''} {p.last_name or ''}".strip() or "—",
        avatar_url=p.avatar_url,
        slug=tp.slug,
        rating_avg=tp.rating_avg,
        reviews_count=tp.reviews_count,
        wallet_balance_dzd=tp.wallet_balance_dzd,
        price_per_session=tp.price_per_session,
        kp_balance=kb.balance if kb else 0,
        badge=tp.badge,
        badge_type=badge_type,
    )


def _fetch_profile(db: Session, user_id: UUID) -> Optional[Profile]:
    return db.exec(select(Profile).where(Profile.id == user_id)).first()


def _fetch_kp(db: Session, user_id: UUID) -> Optional[KpBalance]:
    return db.exec(select(KpBalance).where(KpBalance.user_id == user_id)).first()


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/stats", response_model=PlatformStats)
def get_platform_stats(db: Session = Depends(get_db)):
    """Return platform-wide aggregate numbers shown on the home page."""
    verified_teachers: int = db.exec(
        select(func.count()).select_from(TeacherProfile)
        .where(TeacherProfile.status == "approved", TeacherProfile.verified == True)
    ).one() or 0

    wilayas_covered: int = db.exec(
        select(func.count(distinct(TeacherProfile.teaching_wilaya)))
        .select_from(TeacherProfile)
        .where(
            TeacherProfile.status == "approved",
            TeacherProfile.teaching_wilaya.isnot(None),
        )
    ).one() or 0

    # Count students who completed onboarding
    active_students: int = db.exec(
        select(func.count()).select_from(UserRole)
        .join(Profile, Profile.id == UserRole.user_id)
        .where(UserRole.role == "student", Profile.onboarding_completed == True)
    ).one() or 0

    return PlatformStats(
        verified_teachers=verified_teachers,
        wilayas_covered=wilayas_covered,
        active_students=active_students,
    )


@router.get("/leaderboard", response_model=LeaderboardResponse)
def get_leaderboard(db: Session = Depends(get_db)):
    """
    Return this week's top teachers (by earnings & by rating) and
    top students (by weekly EP earned). Used on the home page hero.
    """
    # ── Top 3 teachers by wallet balance (total earnings held on platform) ──
    top_earning_tps = db.exec(
        select(TeacherProfile)
        .where(TeacherProfile.status == "approved")
        .order_by(TeacherProfile.wallet_balance_dzd.desc())
        .limit(3)
    ).all()

    top_by_earnings: List[TeacherLeaderEntry] = []
    for tp in top_earning_tps:
        p = _fetch_profile(db, tp.user_id)
        if not p:
            continue
        kb = _fetch_kp(db, tp.user_id)
        top_by_earnings.append(_build_teacher_entry(tp, p, kb, "earnings"))

    # ── Top 3 teachers by rating (minimum 3 reviews required) ──
    top_rating_tps = db.exec(
        select(TeacherProfile)
        .where(TeacherProfile.status == "approved", TeacherProfile.reviews_count >= 3)
        .order_by(TeacherProfile.rating_avg.desc(), TeacherProfile.reviews_count.desc())
        .limit(3)
    ).all()

    top_by_rating: List[TeacherLeaderEntry] = []
    for tp in top_rating_tps:
        p = _fetch_profile(db, tp.user_id)
        if not p:
            continue
        kb = _fetch_kp(db, tp.user_id)
        top_by_rating.append(_build_teacher_entry(tp, p, kb, "rating"))

    # Featured = best rated teacher (most reviews as tiebreaker)
    featured_teacher = top_by_rating[0] if top_by_rating else (top_by_earnings[0] if top_by_earnings else None)

    # ── Top 3 students by weekly EP earned ──
    top_student_rows = db.exec(
        select(KpBalance, Profile)
        .join(Profile, Profile.id == KpBalance.user_id)
        .join(UserRole, UserRole.user_id == Profile.id)
        .where(
            UserRole.role == "student",
            Profile.onboarding_completed == True,
        )
        .order_by(KpBalance.week_earned.desc(), KpBalance.balance.desc())
        .limit(3)
    ).all()

    top_students: List[StudentLeaderEntry] = []
    for kb, p in top_student_rows:
        top_students.append(StudentLeaderEntry(
            id=str(p.id),
            full_name=p.full_name or f"{p.first_name or ''} {p.last_name or ''}".strip() or "—",
            avatar_url=p.avatar_url,
            kp_balance=kb.balance,
            kp_week_earned=kb.week_earned,
            level=kb.level,
            wilaya=p.wilaya,
        ))

    return LeaderboardResponse(
        top_by_earnings=top_by_earnings,
        top_by_rating=top_by_rating,
        top_students=top_students,
        featured_teacher=featured_teacher,
    )
