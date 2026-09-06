from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session, select

from app.dependencies import get_db, require_role
from app.models.booking import TutoringSession
from app.models.profile import Profile, TeacherProfile
from app.routers.student_leaderboard import (
    _get_period_bounds,
    _initials,
    _period_label,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Teachers"])


# ─── Response schemas ─────────────────────────────────────────────────────────

class TeacherLeaderboardEntry(BaseModel):
    rank: int
    user_id: str
    name: str
    initials: str
    wilaya: Optional[str]
    rating: float
    active_students: int
    engagement: int  # composite score, 0-100
    is_me: bool


class TeacherLeaderboardResponse(BaseModel):
    entries: List[TeacherLeaderboardEntry]
    me: Optional[TeacherLeaderboardEntry]
    me_rank: Optional[int]
    total_ranked: int
    period_label: str


# ─── Ranking ──────────────────────────────────────────────────────────────────
#
# Chosen metric: a composite "engagement score" (0-100) per teacher, for the
# selected time window (week / month / global), built from three signals
# min-max normalized across the cohort of ranked (approved) teachers:
#
#   engagement = round(100 * (
#       0.45 * (rating_avg / 5.0)                              # quality
#     + 0.35 * (sessions_completed / max_sessions_in_cohort)    # activity
#     + 0.20 * (distinct_active_students / max_students_in_cohort)  # reach
#   ))
#
# rating_avg is the teacher's lifetime average rating (not period-scoped —
# same convention as the student-facing leaderboard's teacher rating, which
# treats rating as "always global accumulated"). Sessions completed and
# distinct active students ARE period-scoped. Only teachers with
# status == "approved" are eligible, and a teacher with zero activity and
# zero rating in the window is excluded from ranking (surfaced to the
# frontend as "not ranked yet").

def _compute_teacher_leaderboard_entries(db: Session, period: str) -> list[dict]:
    import sqlalchemy as sa

    period_start, _ = _get_period_bounds(period)

    active_tps = db.exec(
        select(TeacherProfile).where(TeacherProfile.status == "approved")
    ).all()
    active_ids = {tp.user_id for tp in active_tps}
    teacher_profile_map: dict[UUID, TeacherProfile] = {tp.user_id: tp for tp in active_tps}
    if not active_ids:
        return []

    # -- Sessions completed in period -----------------------------------------
    sess_stmt = (
        select(TutoringSession.teacher_id, sa.func.count().label("cnt"))
        .where(
            TutoringSession.status == "completed",
            TutoringSession.teacher_id.in_(active_ids),  # type: ignore[attr-defined]
        )
    )
    if period_start:
        sess_stmt = sess_stmt.where(TutoringSession.scheduled_at >= period_start)
    sess_stmt = sess_stmt.group_by(TutoringSession.teacher_id)
    sess_rows = db.exec(sess_stmt).all()
    sessions_map: dict[UUID, int] = {r.teacher_id: int(r.cnt) for r in sess_rows}

    # -- Distinct active students in period ------------------------------------
    students_stmt = (
        select(
            TutoringSession.teacher_id,
            sa.func.count(sa.distinct(TutoringSession.student_id)).label("cnt"),
        )
        .where(
            TutoringSession.status == "completed",
            TutoringSession.teacher_id.in_(active_ids),  # type: ignore[attr-defined]
        )
    )
    if period_start:
        students_stmt = students_stmt.where(TutoringSession.scheduled_at >= period_start)
    students_stmt = students_stmt.group_by(TutoringSession.teacher_id)
    students_rows = db.exec(students_stmt).all()
    active_students_map: dict[UUID, int] = {r.teacher_id: int(r.cnt) for r in students_rows}

    # -- Profiles ---------------------------------------------------------------
    profiles = db.exec(
        select(Profile).where(Profile.id.in_(active_ids))  # type: ignore[attr-defined]
    ).all()
    profile_map: dict[UUID, Profile] = {p.id: p for p in profiles}

    # -- Cohort maxima for normalization -----------------------------------------
    max_sessions = max(sessions_map.values(), default=0)
    max_students = max(active_students_map.values(), default=0)

    entries: list[dict] = []
    for uid in active_ids:
        sess = sessions_map.get(uid, 0)
        active_students = active_students_map.get(uid, 0)
        tp = teacher_profile_map.get(uid)
        rating = float(tp.rating_avg) if tp else 0.0

        if sess == 0 and active_students == 0 and rating == 0:
            continue  # brand new / inactive teacher — not ranked yet

        norm_rating = min(rating / 5.0, 1.0)
        norm_sessions = (sess / max_sessions) if max_sessions else 0.0
        norm_students = (active_students / max_students) if max_students else 0.0

        score = round(100 * (0.45 * norm_rating + 0.35 * norm_sessions + 0.20 * norm_students))
        score = max(0, min(100, score))

        p = profile_map.get(uid)
        name = (p.full_name or "").strip() if p else ""
        if not name:
            name = f"{p.first_name or ''} {p.last_name or ''}".strip() if p else "Inconnu"
        wilaya = (p.wilaya if p else None) or (tp.teaching_wilaya if tp else None)

        entries.append({
            "user_id": uid,
            "name": name or "Inconnu",
            "wilaya": wilaya,
            "rating": rating,
            "active_students": active_students,
            "engagement": score,
            "rank": 0,
        })

    entries.sort(key=lambda e: (-e["engagement"], -e["rating"], str(e["user_id"])))

    rank = 1
    for i, e in enumerate(entries):
        if i == 0:
            e["rank"] = 1
        elif e["engagement"] < entries[i - 1]["engagement"]:
            rank = i + 1
            e["rank"] = rank
        else:
            e["rank"] = entries[i - 1]["rank"]

    return entries


# ─── Endpoint ─────────────────────────────────────────────────────────────────

@router.get("/leaderboard", response_model=TeacherLeaderboardResponse)
def get_teacher_leaderboard(
    period: str = "month",
    limit: int = 50,
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    limit = min(limit, 100)
    me_uid = UUID(current_user["id"])

    all_entries = _compute_teacher_leaderboard_entries(db, period)
    total_ranked = len(all_entries)

    me_entry_raw: Optional[dict] = None
    me_rank: Optional[int] = None
    for e in all_entries:
        if e["user_id"] == me_uid:
            me_entry_raw = e
            me_rank = e["rank"]
            break

    top_entries = all_entries[:limit]

    def _to_response(e: dict, is_me: bool) -> TeacherLeaderboardEntry:
        return TeacherLeaderboardEntry(
            rank=e["rank"],
            user_id=str(e["user_id"]),
            name=e["name"],
            initials=_initials(e["name"]),
            wilaya=e["wilaya"],
            rating=e["rating"],
            active_students=e["active_students"],
            engagement=e["engagement"],
            is_me=is_me,
        )

    me_in_top = me_uid in {e["user_id"] for e in top_entries}
    entries = [_to_response(e, e["user_id"] == me_uid) for e in top_entries]

    me_response: Optional[TeacherLeaderboardEntry] = None
    if me_entry_raw and not me_in_top:
        me_response = _to_response(me_entry_raw, True)

    return TeacherLeaderboardResponse(
        entries=entries,
        me=me_response,
        me_rank=me_rank,
        total_ranked=total_ranked,
        period_label=_period_label(period),
    )
