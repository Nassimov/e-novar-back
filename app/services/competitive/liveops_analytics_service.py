from __future__ import annotations

"""
Analytics Service — Competitive Arena Phase 15 (LiveOps).

Every number here is computed live from real rows — no separate analytics
event stream exists on this platform, so DAU/WAU/MAU are honest PROXIES
derived from arena_login_streaks.last_checkin_date (a real signal: someone
who checked in today unambiguously used the Arena today), not a fabricated
metric. Reward distribution reads CompetitiveRewardGrant (Phase 7's
existing audit trail — reused, not duplicated) filtered by this phase's
`source` prefixes ('mission:', 'event:', 'login_day_').
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from uuid import UUID

from sqlmodel import Session, func, select

from app.models.competitive import CompetitiveRewardGrant
from app.models.liveops import ArenaEvent, ArenaEventParticipant, ArenaLoginStreak, ArenaMission, ArenaPlayerMission

LIVEOPS_SOURCE_PREFIXES = ("mission:", "event:", "login_day_", "community_challenge:")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get_mission_analytics(db: Session, *, period: str | None = None) -> Dict[str, Any]:
    query = select(ArenaPlayerMission, ArenaMission).join(ArenaMission, ArenaMission.id == ArenaPlayerMission.mission_id)
    if period:
        query = query.where(ArenaMission.period == period)
    rows = list(db.exec(query).all())

    total = len(rows)
    completed_or_claimed = [r for r, m in rows if r.status in ("completed", "claimed")]
    completion_rate = round((len(completed_or_claimed) / total) * 100, 2) if total else 0.0

    durations_hours = [
        (r.completed_at - r.assigned_at).total_seconds() / 3600
        for r, m in rows if r.completed_at is not None
    ]
    avg_completion_hours = round(sum(durations_hours) / len(durations_hours), 2) if durations_hours else None

    per_mission: Dict[UUID, Dict[str, Any]] = {}
    for r, m in rows:
        entry = per_mission.setdefault(m.id, {"mission_id": m.id, "title": m.title, "assigned": 0, "completed": 0})
        entry["assigned"] += 1
        if r.status in ("completed", "claimed"):
            entry["completed"] += 1
    for entry in per_mission.values():
        entry["completion_rate"] = round((entry["completed"] / entry["assigned"]) * 100, 2) if entry["assigned"] else 0.0

    ranked = sorted(per_mission.values(), key=lambda e: e["completion_rate"], reverse=True)
    return {
        "total_assigned": total,
        "completion_rate_pct": completion_rate,
        "avg_completion_time_hours": avg_completion_hours,
        "most_completed": ranked[:5],
        "least_completed": ranked[-5:][::-1] if len(ranked) > 5 else list(reversed(ranked)),
    }


def get_retention_proxies(db: Session) -> Dict[str, Any]:
    now = _now()
    dau = db.exec(
        select(func.count()).select_from(ArenaLoginStreak).where(ArenaLoginStreak.last_checkin_date == now.date())
    ).one()
    wau = db.exec(
        select(func.count()).select_from(ArenaLoginStreak)
        .where(ArenaLoginStreak.last_checkin_date >= (now - timedelta(days=7)).date())
    ).one()
    mau = db.exec(
        select(func.count()).select_from(ArenaLoginStreak)
        .where(ArenaLoginStreak.last_checkin_date >= (now - timedelta(days=30)).date())
    ).one()
    return {"dau": dau, "wau": wau, "mau": mau}


def get_event_analytics(db: Session) -> Dict[str, Any]:
    events = db.exec(select(ArenaEvent).order_by(ArenaEvent.starts_at.desc()).limit(50)).all()
    out = []
    for event in events:
        participants = db.exec(select(func.count()).select_from(ArenaEventParticipant).where(ArenaEventParticipant.event_id == event.id)).one()
        completed = db.exec(
            select(func.count()).select_from(ArenaEventParticipant)
            .where(ArenaEventParticipant.event_id == event.id).where(ArenaEventParticipant.completed_at.is_not(None))
        ).one()
        out.append({
            "event_id": str(event.id), "name": event.name, "event_type": event.event_type, "status": event.status,
            "participants": participants, "completed": completed,
        })
    return {"events": out}


def get_reward_distribution(db: Session) -> Dict[str, Any]:
    rows = db.exec(select(CompetitiveRewardGrant)).all()
    liveops_rows = [r for r in rows if r.source and r.source.startswith(LIVEOPS_SOURCE_PREFIXES)]
    by_type: Dict[str, Dict[str, int]] = {}
    for r in liveops_rows:
        entry = by_type.setdefault(r.reward_type, {"granted": 0, "recorded_only": 0})
        entry["granted" if r.status == "granted" else "recorded_only"] += 1
    return {"total": len(liveops_rows), "by_reward_type": by_type}
