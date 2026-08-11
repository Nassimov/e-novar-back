from __future__ import annotations

"""
Club Analytics Service — Competitive Arena Phase 11, Part B.

daily/weekly activity reads the club_daily_activity rollup (populated
nightly by app/workers/club_tasks.py's task_rollup_club_daily_activity) —
O(days), never a live scan of club_chat_messages/club_feed_events at full
history, per spec's Performance section ("tens of thousands of clubs").
most_active_members/battle_participation/retention are live-computed but
bounded to a single club_id + a 30-day window, so they stay cheap per-club
even at that scale. "Average online members" reuses the EXISTING generic
`chat:online_users` Redis set (app/routers/messages.py's is_online check) —
a live snapshot, honestly labeled as such (no historical online-time
series exists anywhere in this codebase to average over)."""

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List
from uuid import UUID

from sqlmodel import Session, func, select

from app.core.redis import get_redis_client
from app.models.club import Club, ClubChatMessage, ClubDailyActivity, ClubMember
from app.models.competitive import CompetitiveMatchParticipant
from app.models.profile import Profile
from app.services.club.permission_service import get_club_or_404


def _now() -> datetime:
    return datetime.now(timezone.utc)


def daily_activity(db: Session, club_id: UUID, *, days: int = 30) -> List[Dict[str, Any]]:
    since = date.today() - timedelta(days=days)
    rows = db.exec(
        select(ClubDailyActivity)
        .where(ClubDailyActivity.club_id == club_id)
        .where(ClubDailyActivity.activity_date >= since)
        .order_by(ClubDailyActivity.activity_date.asc())
    ).all()
    return [
        {
            "date": r.activity_date, "message_count": r.message_count, "active_member_count": r.active_member_count,
            "battles_played": r.battles_played, "new_members": r.new_members,
        }
        for r in rows
    ]

def weekly_summary(db: Session, club_id: UUID) -> Dict[str, Any]:
    rows = daily_activity(db, club_id, days=7)
    return {
        "messages": sum(r["message_count"] for r in rows),
        "battles_played": sum(r["battles_played"] for r in rows),
        "new_members": sum(r["new_members"] for r in rows),
        "days_active": sum(1 for r in rows if r["message_count"] > 0 or r["battles_played"] > 0),
    }


def most_active_members(db: Session, club_id: UUID, *, limit: int = 10, days: int = 30) -> List[Dict[str, Any]]:
    since = _now() - timedelta(days=days)
    rows = db.exec(
        select(ClubChatMessage.user_id, func.count().label("message_count"))
        .where(ClubChatMessage.club_id == club_id)
        .where(ClubChatMessage.created_at >= since)
        .where(ClubChatMessage.deleted_at.is_(None))
        .group_by(ClubChatMessage.user_id)
        .order_by(func.count().desc())
        .limit(limit)
    ).all()
    profiles = {p.id: p for p in db.exec(select(Profile).where(Profile.id.in_([r[0] for r in rows]))).all()} if rows else {}
    return [
        {"user_id": uid, "full_name": profiles[uid].full_name if uid in profiles else None, "message_count": count}
        for uid, count in rows
    ]


def average_online_members(db: Session, club_id: UUID) -> int:
    member_ids = db.exec(select(ClubMember.user_id).where(ClubMember.club_id == club_id).where(ClubMember.status == "active")).all()
    if not member_ids:
        return 0
    try:
        r = get_redis_client()
        online = 0
        for uid in member_ids:
            if r.sismember("chat:online_users", str(uid)):
                online += 1
        return online
    except Exception:
        return 0


def retention_30d(db: Session, club_id: UUID) -> float:
    cutoff = _now() - timedelta(days=30)
    cohort = db.exec(
        select(ClubMember).where(ClubMember.club_id == club_id).where(ClubMember.joined_at <= cutoff)
    ).all()
    if not cohort:
        return 0.0
    still_active = sum(1 for m in cohort if m.status == "active")
    return round((still_active / len(cohort)) * 100, 2)


def battle_participation_30d(db: Session, club_id: UUID) -> float:
    cutoff = _now() - timedelta(days=30)
    active_member_ids = set(db.exec(
        select(ClubMember.user_id).where(ClubMember.club_id == club_id).where(ClubMember.status == "active")
    ).all())
    if not active_member_ids:
        return 0.0
    participant_rows = db.exec(
        select(CompetitiveMatchParticipant.user_id)
        .where(CompetitiveMatchParticipant.club_id == club_id)
        .where(CompetitiveMatchParticipant.joined_at >= cutoff)
    ).all()
    participated = active_member_ids.intersection(set(participant_rows))
    return round((len(participated) / len(active_member_ids)) * 100, 2)


def get_club_analytics(db: Session, club_id: UUID) -> Dict[str, Any]:
    get_club_or_404(db, club_id)
    return {
        "daily_activity": daily_activity(db, club_id, days=30),
        "weekly_summary": weekly_summary(db, club_id),
        "most_active_members": most_active_members(db, club_id),
        "average_online_members": average_online_members(db, club_id),
        "retention_30d_pct": retention_30d(db, club_id),
        "battle_participation_30d_pct": battle_participation_30d(db, club_id),
    }
