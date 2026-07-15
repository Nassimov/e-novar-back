"""Admin dashboard KPIs — real data only.

Rewritten from a legacy version that queried app.models.payment.Payment for
revenue (a table nothing in the current booking/payment flow ever inserts
into — Stripe/Chargily/manual payments are tracked directly on
Booking.amount/payment_method/status, see app/routers/student_teachers.py and
app/routers/admin/bookings.py) — that endpoint always returned zero revenue
in production. Revenue here is computed from confirmed Booking rows instead.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, select

from app.dependencies import get_admin_user, get_db
from app.models.admin import PromoCode, PromoRedemption, Report
from app.models.booking import Booking, TutoringSession
from app.models.profile import ParentProfile, Profile, StudentProfile, TeacherProfile
from app.models.review import Review
from app.models.session_validation import SessionValidation
from app.models.store import RewardClaim

router = APIRouter(tags=["admin-stats"])

# A booking represents captured/committed revenue once it's been accepted —
# Stripe is captured, Chargily is paid-before-accept, cash/transfer are
# admin-confirmed. Bookings never move past 'confirmed' on the Booking row
# itself (per-lesson completion lives on TutoringSession instead).
REVENUE_BOOKING_STATUSES = ("confirmed",)


def _pct_delta(current: float, previous: float) -> float:
    if previous <= 0:
        return 100.0 if current > 0 else 0.0
    return round((current - previous) / previous * 100, 1)


@router.get("/")
async def get_platform_stats(
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    now = datetime.utcnow()
    day_ago = now - timedelta(hours=24)
    month_ago = now - timedelta(days=30)
    prev_month_start = now - timedelta(days=60)

    total_users = db.exec(select(Profile)).all()
    total_teachers = len(db.exec(select(TeacherProfile)).all())
    total_students = len(db.exec(select(StudentProfile)).all())
    total_parents = len(db.exec(select(ParentProfile)).all())

    signups_last_24h = sum(1 for p in total_users if p.created_at >= day_ago)

    bookings = db.exec(select(Booking).where(Booking.status.in_(REVENUE_BOOKING_STATUSES))).all()
    revenue_this_month = sum(b.amount for b in bookings if b.created_at >= month_ago)
    revenue_prev_month = sum(b.amount for b in bookings if prev_month_start <= b.created_at < month_ago)

    sessions_completed = db.exec(
        select(TutoringSession).where(TutoringSession.status == "completed")
    ).all()
    sessions_this_month = sum(1 for s in sessions_completed if s.created_at >= month_ago)
    sessions_prev_month = sum(1 for s in sessions_completed if prev_month_start <= s.created_at < month_ago)

    reviews = db.exec(select(Review).where(Review.status == "visible")).all()
    avg_rating_now = round(sum(r.rating for r in reviews) / len(reviews), 2) if reviews else 0.0
    reviews_recent = [r for r in reviews if r.created_at >= month_ago]
    reviews_prev = [r for r in reviews if prev_month_start <= r.created_at < month_ago]
    avg_rating_recent = round(sum(r.rating for r in reviews_recent) / len(reviews_recent), 2) if reviews_recent else avg_rating_now
    avg_rating_prev = round(sum(r.rating for r in reviews_prev) / len(reviews_prev), 2) if reviews_prev else avg_rating_recent

    pending_teacher_approvals = len(
        db.exec(select(TeacherProfile).where(TeacherProfile.status == "pending")).all()
    )
    reported_reviews = len(
        db.exec(select(Report).where(Report.target_type == "review", Report.status == "open")).all()
    )
    pending_rewards = len(db.exec(select(RewardClaim).where(RewardClaim.status == "pending")).all())
    expired_promos = len(
        db.exec(
            select(PromoCode).where(PromoCode.active == True, PromoCode.valid_to != None, PromoCode.valid_to < now)  # noqa: E712,E711
        ).all()
    )
    sessions_needing_review = len(
        db.exec(
            select(SessionValidation).where(SessionValidation.status.in_(["disputed", "admin_review", "expired"]))
        ).all()
    )

    return {
        "total_users": len(total_users),
        "total_teachers": total_teachers,
        "total_students": total_students,
        "total_parents": total_parents,
        "signups_last_24h": signups_last_24h,
        "revenue_this_month_dzd": revenue_this_month,
        "revenue_delta_pct": _pct_delta(revenue_this_month, revenue_prev_month),
        "sessions_completed_this_month": sessions_this_month,
        "sessions_delta_pct": _pct_delta(sessions_this_month, sessions_prev_month),
        "average_rating": avg_rating_recent,
        "average_rating_delta": round(avg_rating_recent - avg_rating_prev, 2),
        "pending_teacher_approvals": pending_teacher_approvals,
        "reported_reviews": reported_reviews,
        "pending_rewards": pending_rewards,
        "expired_promo_codes": expired_promos,
        "sessions_needing_review": sessions_needing_review,
    }


@router.get("/revenue")
async def get_revenue_stats(
    period: str = Query("7j", pattern="^(7j|30j|90j)$"),
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Revenue bucketed for the dashboard chart: 7j → daily, 30j → weekly, 90j → monthly."""
    now = datetime.utcnow()
    if period == "7j":
        since = now - timedelta(days=7)
    elif period == "30j":
        since = now - timedelta(days=28)
    else:
        since = now - timedelta(days=90)

    bookings = db.exec(
        select(Booking).where(Booking.status.in_(REVENUE_BOOKING_STATUSES), Booking.created_at >= since)
    ).all()

    buckets: Dict[str, int] = defaultdict(int)
    labels: List[str] = []

    if period == "7j":
        days_fr = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]
        for i in range(7):
            d = (now - timedelta(days=6 - i)).date()
            labels.append(days_fr[d.weekday()])
        for b in bookings:
            d = b.created_at.date()
            idx = (d - (now - timedelta(days=6)).date()).days
            if 0 <= idx < 7:
                buckets[labels[idx]] += b.amount
    elif period == "30j":
        for i in range(4):
            labels.append(f"S{i + 1}")
        start = now - timedelta(days=28)
        for b in bookings:
            week_idx = min(3, (b.created_at - start).days // 7)
            buckets[labels[week_idx]] += b.amount
    else:
        for i in range(3):
            labels.append(f"Mois {i + 1}")
        start = now - timedelta(days=90)
        for b in bookings:
            month_idx = min(2, (b.created_at - start).days // 30)
            buckets[labels[month_idx]] += b.amount

    total = sum(buckets.values())
    return {
        "period": period,
        "total_revenue_dzd": total,
        "bars": [{"d": label, "v": buckets.get(label, 0)} for label in labels],
    }


@router.get("/top-teachers")
async def get_top_teachers(
    limit: int = Query(4, ge=1, le=20),
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    teachers = db.exec(
        select(TeacherProfile).where(TeacherProfile.reviews_count > 0)
    ).all()
    teachers.sort(key=lambda t: (t.rating_avg, t.reviews_count), reverse=True)
    top = teachers[:limit]

    profile_ids = [t.user_id for t in top]
    profiles = db.exec(select(Profile).where(Profile.id.in_(profile_ids))).all() if profile_ids else []
    profile_map = {p.id: p for p in profiles}

    return [
        {
            "name": (profile_map.get(t.user_id).full_name if profile_map.get(t.user_id) else None) or "—",
            "subject": t.headline or "—",
            "rating": t.rating_avg,
            "sessions": t.students_count,
        }
        for t in top
    ]


@router.get("/activity")
async def get_recent_activity(
    limit: int = Query(15, ge=1, le=50),
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Real recent platform events, merged from several tables (no single
    unified activity-log table exists) and sorted by time, most recent first."""
    events: List[Dict[str, Any]] = []

    recent_signups = db.exec(
        select(Profile).order_by(Profile.created_at.desc()).limit(limit)
    ).all()
    for p in recent_signups:
        events.append({
            "time": p.created_at,
            "title": f"Nouvelle inscription — {p.full_name or p.email or 'Utilisateur'}",
            "icon": "person_add",
            "tone": "brand",
            "detail": p.email or "",
        })

    recent_reports = db.exec(
        select(Report).order_by(Report.created_at.desc()).limit(limit)
    ).all()
    for r in recent_reports:
        events.append({
            "time": r.created_at,
            "title": f"Signalement — {r.target_type} ({r.reason or 'motif non précisé'})",
            "icon": "report",
            "tone": "danger",
            "detail": f"Statut : {r.status}",
        })

    recent_disputes = db.exec(
        select(SessionValidation)
        .where(SessionValidation.dispute_created_at != None)  # noqa: E711
        .order_by(SessionValidation.dispute_created_at.desc())
        .limit(limit)
    ).all()
    for d in recent_disputes:
        events.append({
            "time": d.dispute_created_at,
            "title": f"Litige sur une séance — {d.dispute_reason or 'motif non précisé'}",
            "icon": "gavel",
            "tone": "danger",
            "detail": f"Statut : {d.status}",
        })

    recent_claims = db.exec(
        select(RewardClaim).order_by(RewardClaim.claimed_at.desc()).limit(limit)
    ).all()
    for c in recent_claims:
        events.append({
            "time": c.claimed_at,
            "title": f"Récompense réclamée — {c.cost} EP",
            "icon": "redeem",
            "tone": "gold",
            "detail": f"Statut : {c.status}",
        })

    recent_promo_uses = db.exec(
        select(PromoRedemption).order_by(PromoRedemption.redeemed_at.desc()).limit(limit)
    ).all()
    for pr in recent_promo_uses:
        events.append({
            "time": pr.redeemed_at,
            "title": "Code promo utilisé",
            "icon": "local_offer",
            "tone": "gold",
            "detail": "",
        })

    events.sort(key=lambda e: e["time"], reverse=True)
    top = events[:limit]
    return [
        {
            "time": e["time"].isoformat(),
            "title": e["title"],
            "icon": e["icon"],
            "tone": e["tone"],
            "detail": e["detail"],
        }
        for e in top
    ]
