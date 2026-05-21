from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.gamification import Badge, UserBadge
from app.services.badge_engine import (
    check_and_unlock_badges,
    compute_student_stats,
    badge_progress,
)

router = APIRouter(tags=["Student"])


# ─── Response schemas ─────────────────────────────────────────────────────────

class BadgeOut(BaseModel):
    id: str
    name: str
    description: str
    condition: str
    icon: str
    tier: str
    category: str
    ep_reward: int
    sort_order: int
    # Unlock state
    unlocked: bool
    unlocked_at: Optional[str]
    newly_unlocked: bool   # viewed_at IS NULL → show animation once
    # Progress (for locked badges)
    progress_current: int
    progress_total: int


class BadgesResponse(BaseModel):
    badges: List[BadgeOut]
    total_unlocked: int
    total_badges: int


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/badges", response_model=BadgesResponse)
async def get_student_badges(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])

    # Run the unlock engine first (idempotent — skips already-unlocked)
    stats = compute_student_stats(uid, db)
    check_and_unlock_badges(uid, db, stats=stats)

    # Fetch all active badges
    all_badges = db.exec(
        select(Badge).where(Badge.active == True).order_by(Badge.sort_order)   # noqa: E712
    ).all()

    # Fetch this student's unlocked badges
    user_badge_map: Dict[str, UserBadge] = {}
    ub_rows = db.exec(
        select(UserBadge).where(UserBadge.user_id == uid)
    ).all()
    for ub in ub_rows:
        user_badge_map[ub.badge_id] = ub

    out: List[BadgeOut] = []
    for b in all_badges:
        ub = user_badge_map.get(b.id)
        if ub:
            prog_cur = ub.progress_current or (b.condition_threshold or 0)
            prog_tot = ub.progress_total  or (b.condition_threshold or 1)
        else:
            prog_cur, prog_tot = badge_progress(b, stats)

        out.append(BadgeOut(
            id=b.id,
            name=b.name,
            description=b.description or "",
            condition=b.condition or "",
            icon=b.icon or "military_tech",
            tier=b.tier,
            category=b.category,
            ep_reward=b.ep_reward,
            sort_order=b.sort_order,
            unlocked=ub is not None,
            unlocked_at=ub.unlocked_at.isoformat() if ub else None,
            newly_unlocked=ub is not None and ub.viewed_at is None,
            progress_current=prog_cur,
            progress_total=prog_tot,
        ))

    total_unlocked = len(user_badge_map)
    return BadgesResponse(
        badges=out,
        total_unlocked=total_unlocked,
        total_badges=len(all_badges),
    )


@router.post("/badges/{badge_id}/seen", status_code=204)
async def mark_badge_seen(
    badge_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])
    ub = db.exec(
        select(UserBadge).where(
            UserBadge.user_id  == uid,
            UserBadge.badge_id == badge_id,
        )
    ).first()
    if ub is None:
        raise HTTPException(status_code=404, detail="Badge non débloqué.")
    if ub.viewed_at is None:
        ub.viewed_at = datetime.utcnow()
        db.add(ub)
        db.commit()
