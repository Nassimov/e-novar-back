"""Admin referral management endpoints."""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, select

from app.dependencies import get_admin_user, get_db
from app.models.profile import Profile
from app.models.referral import Referral

router = APIRouter(tags=["Admin – Referrals"])


# ── schemas ───────────────────────────────────────────────────────────────────

from pydantic import BaseModel


class AdminReferralRow(BaseModel):
    id: str
    referrer_id: str
    referrer_name: str
    referee_id: Optional[str]
    referee_name: Optional[str]
    code: str
    referee_role: str
    status: str
    kp_awarded: int
    created_at: str
    validated_at: Optional[str]


class AdminReferralStats(BaseModel):
    total: int
    registered: int
    validated: int
    total_kp_awarded: int
    top_referrers: List[Dict[str, Any]]


# ── helpers ───────────────────────────────────────────────────────────────────

def _name(profiles: dict, uid: Optional[UUID]) -> Optional[str]:
    if uid is None:
        return None
    p = profiles.get(uid)
    return (p.full_name or "Utilisateur") if p else str(uid)


# ── endpoints ─────────────────────────────────────────────────────────────────

@router.get("/stats", response_model=AdminReferralStats)
async def admin_referral_stats(
    _: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    rows = db.exec(select(Referral)).all()

    # Top 10 referrers by validated count
    from collections import defaultdict

    validated = [r for r in rows if r.status == "validated"]
    counts: dict[UUID, dict] = defaultdict(lambda: {"count": 0, "kp": 0})
    for r in validated:
        counts[r.referrer_id]["count"] += 1
        counts[r.referrer_id]["kp"] += r.kp_awarded

    top_ids = sorted(counts.keys(), key=lambda uid: counts[uid]["count"], reverse=True)[:10]
    top_profiles: dict[UUID, Profile] = {}
    if top_ids:
        for p in db.exec(select(Profile).where(Profile.id.in_(top_ids))).all():
            top_profiles[p.id] = p

    top_referrers = [
        {
            "user_id": str(uid),
            "name": (top_profiles.get(uid) or Profile()).full_name or "—",
            "validated_count": counts[uid]["count"],
            "total_kp_given": counts[uid]["kp"],
        }
        for uid in top_ids
    ]

    return AdminReferralStats(
        total=len(rows),
        registered=sum(1 for r in rows if r.status == "registered"),
        validated=len(validated),
        total_kp_awarded=sum(r.kp_awarded for r in rows),
        top_referrers=top_referrers,
    )


@router.get("", response_model=List[AdminReferralRow])
async def admin_list_referrals(
    status: Optional[str] = Query(None, description="registered | validated"),
    role: Optional[str] = Query(None, description="student | teacher | parent"),
    search: Optional[str] = Query(None, description="Name or code search"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    stmt = select(Referral)
    if status:
        stmt = stmt.where(Referral.status == status)
    if role:
        stmt = stmt.where(Referral.referee_role == role)
    if search:
        stmt = stmt.where(Referral.code.ilike(f"%{search}%"))

    stmt = stmt.order_by(Referral.created_at.desc())
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)

    rows = db.exec(stmt).all()

    # Bulk load all profiles in one query
    uids: set[UUID] = set()
    for r in rows:
        uids.add(r.referrer_id)
        if r.referee_id:
            uids.add(r.referee_id)

    profiles: dict[UUID, Profile] = {}
    if uids:
        for p in db.exec(select(Profile).where(Profile.id.in_(list(uids)))).all():
            profiles[p.id] = p

    def _n(uid: Optional[UUID]) -> Optional[str]:
        return _name(profiles, uid)

    return [
        AdminReferralRow(
            id=str(r.id),
            referrer_id=str(r.referrer_id),
            referrer_name=_n(r.referrer_id) or "—",
            referee_id=str(r.referee_id) if r.referee_id else None,
            referee_name=_n(r.referee_id),
            code=r.code,
            referee_role=r.referee_role,
            status=r.status,
            kp_awarded=r.kp_awarded,
            created_at=r.created_at.isoformat(),
            validated_at=r.validated_at.isoformat() if r.validated_at else None,
        )
        for r in rows
    ]
