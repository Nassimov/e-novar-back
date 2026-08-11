from __future__ import annotations

"""
Opponent Search — Competitive Arena Phase 2.

Searches by name or student_code (this platform's closest equivalent to a
"username" — already used for parent-child linking, reused here rather than
inventing a second identifier). Only StudentProfile rows are ever returned
(the INNER JOIN alone excludes teachers/parents/admins). Excludes: self,
either-direction blocks, and students currently suspended from Competitive
Arena (CompetitiveStatistics.suspended_until in the future). Never mutates
data — statistics are read if present, not lazily created, since a search
result list shouldn't write rows for students who were merely looked up.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List
from uuid import UUID

from sqlmodel import Session, select

from app.models.competitive import CompetitiveStatistics
from app.models.profile import Profile, StudentProfile
from app.services.competitive import blocking_service, ranking_service


def search_students(db: Session, *, query: str, current_user_id: UUID, limit: int = 20) -> List[Dict[str, Any]]:
    query = (query or "").strip()
    if len(query) < 2:
        return []

    excluded_ids = set(blocking_service.blocked_user_ids(db, user_id=current_user_id))
    excluded_ids.add(current_user_id)

    like = f"%{query}%"
    rows = db.exec(
        select(Profile, StudentProfile)
        .join(StudentProfile, StudentProfile.user_id == Profile.id)
        .where(
            (Profile.full_name.ilike(like)) | (StudentProfile.student_code.ilike(like))
        )
        .limit(limit + len(excluded_ids))
    ).all()

    now = datetime.now(timezone.utc)
    results: List[Dict[str, Any]] = []
    for profile, student in rows:
        if profile.id in excluded_ids:
            continue
        stats = db.get(CompetitiveStatistics, profile.id)
        if stats and stats.suspended_until and stats.suspended_until > now:
            continue
        win_rate = round((stats.wins / stats.matches_played) * 100, 1) if stats and stats.matches_played else 0.0
        results.append({
            "id": profile.id,
            "full_name": profile.full_name,
            "avatar_url": profile.avatar_url,
            "student_code": student.student_code,
            "rank_tier": ranking_service.get_rank_tier(db, stats.rating if stats else 1000),
            "rating": stats.rating if stats else 1000,
            "win_rate": win_rate,
            "matches_played": stats.matches_played if stats else 0,
        })
        if len(results) >= limit:
            break
    return results
