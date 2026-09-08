"""Teacher visibility boost — "Booster ma visibilité" on teacher/profile.

A teacher spends EP to have their profile promoted in student search/
recommendation results for a fixed number of days. Plans and pricing are
fixed server-side (never trusted from the client, same principle as booking
pricing in app.services.pricing).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlmodel import Session

from app.models.profile import TeacherProfile
from app.services.kp import spend_kp


def get_boost_plans(db: Session) -> dict[int, int]:
    """days -> EP cost. Admin-configurable (business/EP audit, 2026-09-08,
    migration 110) via PlatformSettings.kp_boost_cost_*d — was a hardcoded
    dict; defaults reproduce the previous values exactly."""
    from app.services.pricing import get_platform_settings

    s = get_platform_settings(db)
    return {7: s.kp_boost_cost_7d, 30: s.kp_boost_cost_30d, 90: s.kp_boost_cost_90d}


def is_boost_active(tp: TeacherProfile) -> bool:
    """Whether tp's visibility boost is currently in effect. Checked at every
    read site instead of relying on a cron job to flip `sponsored` back off —
    a lapsed boost stops affecting ranking immediately."""
    if not tp.sponsored:
        return False
    if tp.boost_expires_at is None:
        return False
    expires = tp.boost_expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires > datetime.now(timezone.utc)


def activate_boost(
    tp: TeacherProfile, days: int, db: Session, *, idempotency_key: Optional[str] = None,
) -> TeacherProfile:
    """Spend EP and activate (or extend) the teacher's visibility boost.
    Raises ValueError on an invalid plan or insufficient EP balance.

    idempotency_key is optional and caller-supplied (the frontend generates
    one per purchase *attempt* — stable across a double-click/retry of that
    same attempt, refreshed on the next one) — buying the same plan again
    in a genuinely separate attempt still stacks more boost time (see the
    extend-from-current-expiry logic below); only a replay of the exact
    same attempt is deduped, via was_spent below."""
    plans = get_boost_plans(db)
    if days not in plans:
        raise ValueError(f"Offre de boost invalide (choix valides : {sorted(plans)} jours).")
    cost = plans[days]

    # spend_kp raises ValueError itself if the balance is insufficient —
    # let it propagate, the caller maps it to a 400.
    _account, was_spent = spend_kp(
        tp.user_id, cost, f"Boost visibilité {days} jours", db, idempotency_key=idempotency_key,
    )
    if not was_spent:
        # Deduped replay of the same attempt — the first call already
        # extended boost_expires_at, doing it again would double-credit
        # the SAME payment with two extensions.
        return tp

    now = datetime.now(timezone.utc)
    # Extend from the current expiry if a boost is already active, so buying
    # more time never shortens what was already paid for.
    base = tp.boost_expires_at if (tp.boost_expires_at and is_boost_active(tp)) else now
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)

    tp.sponsored = True
    tp.boost_expires_at = base + timedelta(days=days)
    db.add(tp)
    db.commit()
    db.refresh(tp)
    return tp


def clear_expired(tp: TeacherProfile, db: Session) -> None:
    """Self-heal: flip `sponsored` back off once its expiry has passed, the
    next time this teacher's own profile is loaded/saved. Ranking reads
    don't depend on this — they call is_boost_active() directly — this just
    keeps the stored `sponsored` bool from looking stale to anything that
    still reads it as a plain flag (e.g. legacy response fields)."""
    if tp.sponsored and not is_boost_active(tp):
        tp.sponsored = False
        db.add(tp)
        db.commit()
        db.refresh(tp)
