from __future__ import annotations

"""
Metrics Service — Competitive Arena Phase 16.

Extends Phase 15's liveops_analytics_service (DAU/WAU/MAU, mission
completion) with the platform-health metrics the spec names: match
duration, queue time, answer time, match/tournament/BR completion &
participation. Every number is computed live from tables that already
exist — no separate metrics-collection pipeline. A real Prometheus/
Datadog EXPORTER is genuinely out of reach here (no such backend is
provisioned for this Railway deployment) — this service is the data
source a future exporter would read from, not a fabricated integration.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from sqlmodel import Session, func, select

from app.models.competitive import (
    CompetitiveMatch,
    CompetitiveMatchPlayerStats,
    CompetitiveQueueEntry,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get_match_metrics(db: Session, *, since_hours: int = 24) -> Dict[str, Any]:
    since = _now() - timedelta(hours=since_hours)
    matches = list(db.exec(select(CompetitiveMatch).where(CompetitiveMatch.created_at >= since)).all())
    if not matches:
        return {"total": 0, "completion_rate_pct": 0.0, "avg_duration_sec": None, "by_status": {}}

    by_status: Dict[str, int] = {}
    durations = []
    for m in matches:
        by_status[m.status] = by_status.get(m.status, 0) + 1
        if m.started_at and m.completed_at:
            durations.append((m.completed_at - m.started_at).total_seconds())

    completed = by_status.get("completed", 0) + by_status.get("abandoned", 0)
    completion_rate = round((completed / len(matches)) * 100, 2) if matches else 0.0
    avg_duration = round(sum(durations) / len(durations), 1) if durations else None

    return {
        "total": len(matches), "completion_rate_pct": completion_rate,
        "avg_duration_sec": avg_duration, "by_status": by_status,
    }


def get_queue_metrics(db: Session, *, since_hours: int = 24) -> Dict[str, Any]:
    since = _now() - timedelta(hours=since_hours)
    entries = list(db.exec(
        select(CompetitiveQueueEntry)
        .where(CompetitiveQueueEntry.created_at >= since)
        .where(CompetitiveQueueEntry.accepted_at.is_not(None))
    ).all())
    if not entries:
        return {"matched_count": 0, "avg_queue_time_sec": None}
    waits = [(e.accepted_at - e.search_started_at).total_seconds() for e in entries]
    return {"matched_count": len(entries), "avg_queue_time_sec": round(sum(waits) / len(waits), 1)}


def get_answer_time_metrics(db: Session, *, since_hours: int = 24) -> Dict[str, Any]:
    since = _now() - timedelta(hours=since_hours)
    rows = list(db.exec(
        select(CompetitiveMatchPlayerStats.avg_response_time_ms)
        .join(CompetitiveMatch, CompetitiveMatch.id == CompetitiveMatchPlayerStats.match_id)
        .where(CompetitiveMatch.completed_at >= since)
        .where(CompetitiveMatchPlayerStats.avg_response_time_ms.is_not(None))
    ).all())
    if not rows:
        return {"sample_size": 0, "avg_answer_time_ms": None}
    return {"sample_size": len(rows), "avg_answer_time_ms": round(sum(rows) / len(rows), 1)}


def get_participation_metrics(db: Session, *, since_hours: int = 24) -> Dict[str, Any]:
    since = _now() - timedelta(hours=since_hours)
    by_type = dict(db.exec(
        select(CompetitiveMatch.match_type, func.count())
        .where(CompetitiveMatch.created_at >= since)
        .group_by(CompetitiveMatch.match_type)
    ).all())
    return {"matches_by_type": by_type}


def get_realtime_snapshot(db: Session) -> Dict[str, Any]:
    """Cheap, present-tense counters — safe to poll frequently for a live
    admin dashboard (each is a small indexed COUNT, not a table scan)."""
    active_matches = db.exec(
        select(func.count()).select_from(CompetitiveMatch).where(CompetitiveMatch.status == "in_progress")
    ).one()
    searching = db.exec(
        select(func.count()).select_from(CompetitiveQueueEntry).where(CompetitiveQueueEntry.status == "searching")
    ).one()
    today = _now().date().isoformat()
    matches_completed_today = 0
    try:
        from app.core.redis import get_redis_client
        val = get_redis_client().get(f"arena:metrics:matches_completed:{today}")
        matches_completed_today = int(val) if val else 0
    except Exception:
        pass
    return {
        "active_matches": active_matches, "players_in_queue": searching,
        "matches_completed_today": matches_completed_today,
    }
