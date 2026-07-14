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

# days -> EP cost. Mirrors what was previously only shown client-side.
BOOST_PLANS: dict[int, int] = {
    7: 100,
    30: 350,
    90: 900,
}


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


def activate_boost(tp: TeacherProfile, days: int, db: Session) -> TeacherProfile:
    """Spend EP and activate (or extend) the teacher's visibility boost.
    Raises ValueError on an invalid plan or insufficient EP balance."""
    if days not in BOOST_PLANS:
        raise ValueError(f"Offre de boost invalide (choix valides : {sorted(BOOST_PLANS)} jours).")
    cost = BOOST_PLANS[days]

    # spend_kp raises ValueError itself if the balance is insufficient —
    # let it propagate, the caller maps it to a 400.
    spend_kp(tp.user_id, cost, f"Boost visibilité {days} jours", db)

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
