"""
Promo code eligibility engine.

Each `target_role` value maps to a specific audience.
All checks are data-driven (no hardcoded user IDs).

Pre-fetch pattern: callers that check multiple codes should build a
`TargetingContext` once and reuse it — avoids N+1 DB round-trips.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from sqlalchemy import or_
from sqlmodel import Session, select

from app.models.booking import Booking
from app.models.kp import KpBalance
from app.models.profile import StudentProfile

# ── tuneable constants ────────────────────────────────────────────────────────

# "Top students" = KP level >= this threshold (level 3 starts at 1 500 EP total)
TOP_STUDENT_MIN_LEVEL: int = 3

# "Inactive" = no booking created in the last N days
INACTIVE_DAYS: int = 30

# "Low KP" = balance strictly below this value
LOW_KP_MAX_BALANCE: int = 200

# All target_role values accepted by the system
VALID_TARGET_ROLES = frozenset({
    "all",
    "student",
    "teacher",
    "parent",
    "student_lycee",
    "student_bac",
    "student_inactive_30d",
    "student_top",
    "student_low_kp",
})

# Human-readable labels (used by validation error messages)
TARGET_LABELS: dict[str, str] = {
    "all":                  "Tous les utilisateurs",
    "student":              "Tous les étudiants",
    "teacher":              "Tous les enseignants",
    "parent":               "Tous les parents",
    "student_lycee":        "Étudiants lycée (1AS → 3AS)",
    "student_bac":          "Étudiants BAC (Terminale / 3ème AS)",
    "student_inactive_30d": "Étudiants sans séance depuis 30 jours",
    "student_top":          f"Meilleurs étudiants (niveau EP ≥ {TOP_STUDENT_MIN_LEVEL})",
    "student_low_kp":       f"Étudiants avec moins de {LOW_KP_MAX_BALANCE} EP",
}


# ── context object (pre-fetched per request) ──────────────────────────────────

@dataclass
class TargetingContext:
    """
    Holds DB data needed for eligibility checks, fetched once per request.
    Build it with `build_context()` before iterating over multiple codes.
    """
    user_id: UUID
    user_role: str
    student_profile: Optional[StudentProfile] = None
    kp_balance: Optional[KpBalance] = None
    has_recent_booking: bool = False   # True = has a booking in the last INACTIVE_DAYS


def build_context(user_id: UUID, user_role: str, db: Session) -> TargetingContext:
    """
    Pre-fetch all data needed for any targeting check.
    Called once per API request, regardless of how many codes are being evaluated.
    """
    ctx = TargetingContext(user_id=user_id, user_role=user_role)

    if user_role == "student":
        ctx.student_profile = db.exec(
            select(StudentProfile).where(StudentProfile.user_id == user_id)
        ).first()

        ctx.kp_balance = db.exec(
            select(KpBalance).where(KpBalance.user_id == user_id)
        ).first()

        cutoff = dt.date.today() - dt.timedelta(days=INACTIVE_DAYS)
        recent = db.exec(
            select(Booking)
            .where(
                Booking.student_id == user_id,
                or_(
                    Booking.status == "confirmed",
                    Booking.status == "completed",
                    Booking.status == "pending",
                ),
                Booking.booking_date >= cutoff,
            )
            .limit(1)
        ).first()
        ctx.has_recent_booking = recent is not None

    return ctx


# ── eligibility check ─────────────────────────────────────────────────────────

def check_eligibility(
    ctx: TargetingContext,
    target_role: str,
) -> tuple[bool, str]:
    """
    Return (eligible, reason_if_not).
    `reason_if_not` is a user-facing French error message.
    """
    if target_role == "all":
        return True, ""

    # Basic role checks (no extra DB data needed)
    if target_role == "student":
        if ctx.user_role == "student":
            return True, ""
        return False, "Ce code est réservé aux étudiants."

    if target_role == "teacher":
        if ctx.user_role == "teacher":
            return True, ""
        return False, "Ce code est réservé aux enseignants."

    if target_role == "parent":
        if ctx.user_role == "parent":
            return True, ""
        return False, "Ce code est réservé aux parents."

    # Sub-student checks
    if not target_role.startswith("student_"):
        return False, "Ciblage de code inconnu."

    if ctx.user_role != "student":
        return False, "Ce code est réservé aux étudiants."

    if target_role == "student_lycee":
        sp = ctx.student_profile
        if sp and sp.level_main == "lycee":
            return True, ""
        return False, "Ce code est réservé aux étudiants du lycée (1ère AS → 3ème AS)."

    if target_role == "student_bac":
        sp = ctx.student_profile
        if sp and sp.level_main == "lycee":
            detail = (sp.level_detail or "").lower()
            # "3ème AS", "3as", "terminale", "bac", or any detail containing "3"
            if any(kw in detail for kw in ("3", "terminal", "bac")):
                return True, ""
        return False, "Ce code est réservé aux étudiants de Terminale (3ème AS — préparation BAC)."

    if target_role == "student_inactive_30d":
        if not ctx.has_recent_booking:
            return True, ""
        return False, f"Ce code est réservé aux étudiants sans séance depuis {INACTIVE_DAYS} jours."

    if target_role == "student_top":
        kp = ctx.kp_balance
        if kp and kp.level >= TOP_STUDENT_MIN_LEVEL:
            return True, ""
        return False, (
            f"Ce code est réservé aux meilleurs étudiants "
            f"(niveau EP ≥ {TOP_STUDENT_MIN_LEVEL})."
        )

    if target_role == "student_low_kp":
        balance = ctx.kp_balance.balance if ctx.kp_balance else 0
        if balance < LOW_KP_MAX_BALANCE:
            return True, ""
        return False, f"Ce code est réservé aux étudiants avec moins de {LOW_KP_MAX_BALANCE} EP."

    return False, "Ciblage de code inconnu."
