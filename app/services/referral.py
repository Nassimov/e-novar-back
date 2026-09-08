"""
Referral service — shared logic used by the referrals router and by
booking/session completion hooks.
"""
from __future__ import annotations

import random
import string
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlmodel import Session, select


def referral_kp_tables(db: Session) -> tuple[dict, dict]:
    """KP rewards by referee role — admin-configurable (business/EP audit,
    2026-09-08, migration 110) via PlatformSettings.kp_referral_*. Was a
    hardcoded dict; defaults reproduce the previous hardcoded values
    exactly, so nothing changes until an admin edits them."""
    from app.services.pricing import get_platform_settings

    s = get_platform_settings(db)
    referrer_kp = {
        "student": s.kp_referral_referrer_student,
        "teacher": s.kp_referral_referrer_teacher,
        "parent": s.kp_referral_referrer_parent,
    }
    referee_kp = {
        "student": s.kp_referral_referee_student,
        "teacher": s.kp_referral_referee_teacher,
        "parent": s.kp_referral_referee_parent,
    }
    return referrer_kp, referee_kp


def _generate_code(name: str, role: str) -> str:
    """Derive a short memorable code from the user's name and role."""
    prefix = (name or "").split()[0].upper()[:5].strip() or "ENOV"
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    tag = "T" if role == "teacher" else ("P" if role == "parent" else "")
    return f"{prefix}{tag}-{suffix}"


def get_or_create_code(user_id: UUID, role: str, db: Session) -> str:
    """Return the user's referral code, creating one if they don't have one yet."""
    from app.models.profile import Profile

    profile = db.exec(select(Profile).where(Profile.id == user_id)).first()
    if profile is None:
        raise ValueError("Profile not found")

    if profile.referral_code:
        return profile.referral_code

    # Generate a unique code
    for _ in range(20):
        code = _generate_code(profile.full_name or "", role)
        if not db.exec(select(Profile).where(Profile.referral_code == code)).first():
            profile.referral_code = code
            db.add(profile)
            db.commit()
            db.refresh(profile)
            return code

    raise RuntimeError("Could not generate a unique referral code")


def apply_referral_code(
    referee_id: UUID,
    referee_role: str,
    code: str,
    db: Session,
    *,
    referee_ip: Optional[str] = None,
) -> dict:
    """
    Apply a referral code for a newly registered user.

    Rules:
    - A user can only be referred once.
    - A user cannot use their own code.
    - The code must belong to an active profile.

    Returns a dict with the referrer info and the KP awarded to the referee.
    Raises ValueError on any violation.

    referee_ip: best-effort client IP (Point 5.3 — multi-account
    monitoring), stored purely for GET /admin/referrals/suspicious-ips to
    surface for manual review. Never used to block anything here.
    """
    from app.models.profile import Profile
    from app.models.referral import Referral
    from app.services.kp import award_kp, KpSource

    # Check referee hasn't already been referred
    existing = db.exec(
        select(Referral).where(Referral.referee_id == referee_id)
    ).first()
    if existing:
        raise ValueError("Ce code a déjà été appliqué à ton compte.")

    # Look up the code owner
    referrer_profile = db.exec(
        select(Profile).where(Profile.referral_code == code.upper())
    ).first()
    if referrer_profile is None:
        raise ValueError("Code de parrainage invalide.")

    if referrer_profile.id == referee_id:
        raise ValueError("Tu ne peux pas utiliser ton propre code.")

    # Determine kp to give referee immediately
    _referrer_kp, referee_kp_table = referral_kp_tables(db)
    kp_referee = referee_kp_table.get(referee_role, 100)

    row = Referral(
        referrer_id=referrer_profile.id,
        referee_id=referee_id,
        code=code.upper(),
        status="registered",
        referee_role=referee_role,
        kp_awarded=0,   # will be set at validation
        referee_ip=referee_ip,
    )
    db.add(row)
    db.flush()

    # Award KP to referee immediately (welcome bonus). ref_type/ref_id makes
    # this idempotent per Referral row — a retried request can't grant the
    # welcome bonus twice (a user can only be referred once anyway, but this
    # also protects against a retry landing between the two checks above).
    award_kp(
        referee_id,
        kp_referee,
        KpSource.referral,
        f"Bonus parrainage — inscription via {referrer_profile.full_name or 'un ami'}",
        db,
        ref_type="referral_welcome",
        ref_id=row.id,
    )

    db.commit()
    return {
        "kp_earned": kp_referee,
        "referrer_name": referrer_profile.full_name or "Utilisateur",
    }


def validate_referral_for_user(user_id: UUID, db: Session) -> bool:
    """
    Called when a user (student OR teacher) completes their first session/booking.
    Finds any unvalidated referral for this user and awards KP to their referrer.
    Returns True if a referral was validated.
    """
    from app.models.referral import Referral
    from app.services.kp import award_kp, KpSource

    row = db.exec(
        select(Referral).where(
            Referral.referee_id == user_id,
            Referral.status == "registered",
        )
    ).first()
    if row is None:
        return False

    referrer_kp_table, _referee_kp = referral_kp_tables(db)
    kp_referrer = referrer_kp_table.get(row.referee_role, 200)

    row.status = "validated"
    row.validated_at = datetime.now(timezone.utc)
    row.kp_awarded = kp_referrer
    db.add(row)
    db.flush()

    award_kp(
        row.referrer_id,
        kp_referrer,
        KpSource.referral,
        f"Parrainage validé — {row.referee_role}",
        db,
        ref_type="referral_validated",
        ref_id=row.id,
    )

    db.commit()
    return True
