"""
Central entitlement service — checks and consumes UserEntitlement rows.

All write operations (consume_*) use SELECT FOR UPDATE to prevent
concurrent double-consumption. Callers are responsible for db.commit().
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlmodel import Session, select

from app.models.store import UserEntitlement


def _get_active(
    user_id: UUID,
    effect_type: str,
    db: Session,
    lock: bool = False,
) -> Optional[UserEntitlement]:
    """Return the oldest active, non-expired entitlement for the given effect type."""
    stmt = (
        select(UserEntitlement)
        .where(
            UserEntitlement.user_id == user_id,
            UserEntitlement.effect_type == effect_type,
            UserEntitlement.status == "active",
        )
        .order_by(UserEntitlement.activated_at)
    )
    if lock:
        stmt = stmt.with_for_update(skip_locked=True)

    now = datetime.now(timezone.utc)
    for e in db.exec(stmt).all():
        if e.expires_at is None or e.expires_at > now:
            return e
    return None


# ── Read helpers ──────────────────────────────────────────────────────────────

def get_active_ep_boost(user_id: UUID, db: Session) -> Optional[UserEntitlement]:
    return _get_active(user_id, "ep_boost", db)


def get_active_premium(user_id: UUID, db: Session) -> Optional[UserEntitlement]:
    return _get_active(user_id, "premium", db)


def get_active_ai_boost(user_id: UUID, db: Session) -> Optional[UserEntitlement]:
    return _get_active(user_id, "ai_boost", db, lock=True)


def get_active_streak_shield(user_id: UUID, db: Session) -> Optional[UserEntitlement]:
    return _get_active(user_id, "streak_shield", db, lock=True)


# ── Consume helpers ───────────────────────────────────────────────────────────

def consume_ai_boost_question(entitlement: UserEntitlement, db: Session) -> None:
    """Decrement one question from the ai_boost pool. Marks as 'used' when exhausted."""
    now = datetime.now(timezone.utc)
    config = dict(entitlement.effect_config or {})
    # remaining is set on creation; fall back to questions if missing
    remaining = max(0, int(config.get("remaining", config.get("questions", 1))) - 1)
    config["remaining"] = remaining
    entitlement.effect_config = config
    if remaining <= 0:
        entitlement.status = "used"
        entitlement.used_at = now
    db.add(entitlement)


def consume_streak_shield(entitlement: UserEntitlement, db: Session) -> None:
    """Mark streak_shield as used (one-time protection)."""
    now = datetime.now(timezone.utc)
    entitlement.status = "used"
    entitlement.used_at = now
    db.add(entitlement)


# ── Lazy expiry sweep ─────────────────────────────────────────────────────────

def expire_stale_entitlements(user_id: UUID, db: Session) -> None:
    """Mark all expired-but-still-active entitlements as 'expired'. Called lazily per request."""
    now = datetime.now(timezone.utc)
    stale = db.exec(
        select(UserEntitlement).where(
            UserEntitlement.user_id == user_id,
            UserEntitlement.status == "active",
            UserEntitlement.expires_at.isnot(None),
            UserEntitlement.expires_at < now,
        )
    ).all()
    for e in stale:
        e.status = "expired"
        db.add(e)
    if stale:
        db.commit()
