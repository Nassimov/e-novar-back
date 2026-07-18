from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.booking import TutoringSession
from app.models.catalog import Level, Subject
from app.models.evaluation import Evaluation
from app.models.kp import KpBalance, KpTransaction
from app.models.profile import Profile, TeacherProfile, UserRole
from app.models.progress import LeaderboardRankSnapshot

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Student"])


# ─── Response schemas ─────────────────────────────────────────────────────────

class LeaderboardEntry(BaseModel):
    rank: int
    user_id: str
    name: str
    initials: str
    wilaya: Optional[str]
    kp_level: int
    kp: int
    sessions: int
    rating: float
    score: float
    is_me: bool


class LeaderboardResponse(BaseModel):
    entries: List[LeaderboardEntry]
    me: Optional[LeaderboardEntry]
    me_rank: Optional[int]
    total_ranked: int
    period_label: str


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_period_bounds(period: str) -> tuple[Optional[datetime], Optional[datetime]]:
    now = datetime.now(tz=timezone.utc)
    if period == "week":
        start = now.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) - __import__("datetime").timedelta(days=now.weekday())
        return start, None
    if period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return start, None
    return None, None


def _period_key(period: str) -> tuple[str, str]:
    now = datetime.now(tz=timezone.utc)
    if period == "week":
        iso = now.isocalendar()
        return "weekly", f"{iso[0]}-W{iso[1]:02d}"
    if period == "month":
        return "monthly", f"{now.year}-{now.month:02d}"
    return "global", "all"


_FRENCH_MONTHS = [
    "", "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]
_FRENCH_MONTHS_CAP = [
    "", "Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
    "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre",
]


def _period_label(period: str) -> str:
    now = datetime.now(tz=timezone.utc)
    if period == "week":
        iso = now.isocalendar()
        # Monday of the ISO week
        import datetime as _dt
        monday = _dt.date.fromisocalendar(iso[0], iso[1], 1)
        return f"Semaine du {monday.day} {_FRENCH_MONTHS[monday.month]} {monday.year}"
    if period == "month":
        return f"{_FRENCH_MONTHS_CAP[now.month]} {now.year}"
    return "Classement général"


def _initials(name: str) -> str:
    parts = name.strip().split()
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    if parts and len(parts[0]) >= 2:
        return parts[0][:2].upper()
    return (parts[0][0] if parts else "?").upper()


def _assign_dense_ranks(entries: list) -> list:
    rank = 1
    for i, e in enumerate(entries):
        if i == 0:
            e["rank"] = 1
        elif e["score"] < entries[i - 1]["score"]:
            rank = i + 1
            e["rank"] = rank
        else:
            e["rank"] = entries[i - 1]["rank"]
    return entries


# ─── Student ranking ──────────────────────────────────────────────────────────

def _compute_student_entries(
    db: Session,
    period: str,
    sort_by: str,
    subject: Optional[str],
    level: Optional[str],
) -> list[dict]:
    import sqlalchemy as sa

    period_start, _ = _get_period_bounds(period)

    # -- Resolve optional subject/level filter IDs ----------------------------
    subject_id: Optional[UUID] = None
    level_id: Optional[UUID] = None
    if subject:
        subj_row = db.exec(select(Subject).where(Subject.name == subject)).first()
        if subj_row:
            subject_id = subj_row.id
    if level:
        lvl_row = db.exec(select(Level).where(Level.code == level)).first()
        if lvl_row:
            level_id = lvl_row.id

    # -- Fetch all student user_ids -------------------------------------------
    student_roles = db.exec(
        select(UserRole).where(UserRole.role == "student")
    ).all()
    student_ids = {ur.user_id for ur in student_roles}
    if not student_ids:
        return []

    # -- EP metric -----------------------------------------------------------
    kp_map: dict[UUID, int] = {}
    if period == "global":
        balances = db.exec(
            select(KpBalance).where(KpBalance.user_id.in_(student_ids))  # type: ignore[attr-defined]
        ).all()
        for b in balances:
            kp_map[b.user_id] = b.total_earned
    else:
        stmt = (
            select(KpTransaction.user_id, sa.func.sum(KpTransaction.amount).label("total"))
            .where(
                KpTransaction.user_id.in_(student_ids),  # type: ignore[attr-defined]
                KpTransaction.amount > 0,
                KpTransaction.created_at >= period_start,
            )
            .group_by(KpTransaction.user_id)
        )
        rows = db.exec(stmt).all()
        for r in rows:
            kp_map[r.user_id] = int(r.total)

    # -- Sessions metric ------------------------------------------------------
    sess_stmt = (
        select(TutoringSession.student_id, sa.func.count().label("cnt"))
        .where(TutoringSession.status == "completed")
        .where(TutoringSession.student_id.in_(student_ids))  # type: ignore[attr-defined]
    )
    if period_start:
        sess_stmt = sess_stmt.where(TutoringSession.scheduled_at >= period_start)
    if subject_id:
        sess_stmt = sess_stmt.where(TutoringSession.subject_id == subject_id)
    if level_id:
        sess_stmt = sess_stmt.where(TutoringSession.level_id == level_id)
    sess_stmt = sess_stmt.group_by(TutoringSession.student_id)
    sess_rows = db.exec(sess_stmt).all()
    sessions_map: dict[UUID, int] = {r.student_id: int(r.cnt) for r in sess_rows}

    # -- Rating metric (AVG eval score /4 → 0-5 scale) -----------------------
    eval_stmt = (
        select(
            Evaluation.student_id,
            sa.func.avg(Evaluation.score_global).label("avg_score"),
        )
        .where(
            Evaluation.student_id.in_(student_ids),  # type: ignore[attr-defined]
            Evaluation.score_global.isnot(None),
        )
    )
    if period_start:
        eval_stmt = eval_stmt.where(Evaluation.created_at >= period_start)
    eval_stmt = eval_stmt.group_by(Evaluation.student_id)
    eval_rows = db.exec(eval_stmt).all()
    rating_map: dict[UUID, float] = {
        r.student_id: round(float(r.avg_score) / 4.0, 2) for r in eval_rows
    }

    # -- KP levels -----------------------------------------------------------
    all_balances = db.exec(
        select(KpBalance).where(KpBalance.user_id.in_(student_ids))  # type: ignore[attr-defined]
    ).all()
    kp_level_map: dict[UUID, int] = {b.user_id: b.level for b in all_balances}

    # -- Profiles ------------------------------------------------------------
    profiles = db.exec(
        select(Profile).where(Profile.id.in_(student_ids))  # type: ignore[attr-defined]
    ).all()
    profile_map: dict[UUID, Profile] = {p.id: p for p in profiles}

    # -- Collect all users with any activity ----------------------------------
    active_ids = set(kp_map.keys()) | set(sessions_map.keys()) | set(rating_map.keys())

    entries: list[dict] = []
    for uid in active_ids:
        kp = kp_map.get(uid, 0)
        sess = sessions_map.get(uid, 0)
        rat = rating_map.get(uid, 0.0)
        if sort_by == "sessions":
            score = float(sess)
        elif sort_by == "rating":
            score = rat
        else:
            score = float(kp)
        if score == 0:
            continue
        p = profile_map.get(uid)
        name = (p.full_name or "").strip() if p else ""
        if not name:
            name = f"{p.first_name or ''} {p.last_name or ''}".strip() if p else "Inconnu"
        entries.append({
            "user_id": uid,
            "name": name or "Inconnu",
            "wilaya": p.wilaya if p else None,
            "kp_level": kp_level_map.get(uid, 1),
            "kp": kp,
            "sessions": sess,
            "rating": rat,
            "score": score,
            "rank": 0,
        })

    entries.sort(key=lambda e: (-e["score"], str(e["user_id"])))
    _assign_dense_ranks(entries)
    return entries


# ─── Teacher ranking ─────────────────────────────────────────────────────────

def _compute_teacher_entries(
    db: Session,
    period: str,
    sort_by: str,
) -> list[dict]:
    import sqlalchemy as sa

    period_start, _ = _get_period_bounds(period)

    teacher_roles = db.exec(
        select(UserRole).where(UserRole.role == "teacher")
    ).all()
    teacher_ids = {ur.user_id for ur in teacher_roles}
    if not teacher_ids:
        return []

    active_tps = db.exec(
        select(TeacherProfile).where(
            TeacherProfile.user_id.in_(teacher_ids),  # type: ignore[attr-defined]
            TeacherProfile.status == "approved",
        )
    ).all()
    active_ids = {tp.user_id for tp in active_tps}
    teacher_profile_map: dict[UUID, TeacherProfile] = {tp.user_id: tp for tp in active_tps}
    if not active_ids:
        return []

    # -- EP ------------------------------------------------------------------
    kp_map: dict[UUID, int] = {}
    if period == "global":
        balances = db.exec(
            select(KpBalance).where(KpBalance.user_id.in_(active_ids))  # type: ignore[attr-defined]
        ).all()
        for b in balances:
            kp_map[b.user_id] = b.total_earned
    else:
        stmt = (
            select(KpTransaction.user_id, sa.func.sum(KpTransaction.amount).label("total"))
            .where(
                KpTransaction.user_id.in_(active_ids),  # type: ignore[attr-defined]
                KpTransaction.amount > 0,
                KpTransaction.created_at >= period_start,
            )
            .group_by(KpTransaction.user_id)
        )
        rows = db.exec(stmt).all()
        for r in rows:
            kp_map[r.user_id] = int(r.total)

    # -- Sessions ------------------------------------------------------------
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

    # -- Rating (always global accumulated) ----------------------------------
    rating_map: dict[UUID, float] = {
        tp.user_id: float(tp.rating_avg) for tp in active_tps
    }

    # -- KP levels -----------------------------------------------------------
    all_balances = db.exec(
        select(KpBalance).where(KpBalance.user_id.in_(active_ids))  # type: ignore[attr-defined]
    ).all()
    kp_level_map: dict[UUID, int] = {b.user_id: b.level for b in all_balances}

    # -- Profiles ------------------------------------------------------------
    profiles = db.exec(
        select(Profile).where(Profile.id.in_(active_ids))  # type: ignore[attr-defined]
    ).all()
    profile_map: dict[UUID, Profile] = {p.id: p for p in profiles}

    active_entry_ids = set(kp_map.keys()) | set(sessions_map.keys()) | set(rating_map.keys())

    entries: list[dict] = []
    for uid in active_entry_ids:
        kp = kp_map.get(uid, 0)
        sess = sessions_map.get(uid, 0)
        rat = rating_map.get(uid, 0.0)
        if sort_by == "sessions":
            score = float(sess)
        elif sort_by == "rating":
            score = rat
        else:
            score = float(kp)
        if score == 0:
            continue
        p = profile_map.get(uid)
        name = (p.full_name or "").strip() if p else ""
        if not name:
            name = f"{p.first_name or ''} {p.last_name or ''}".strip() if p else "Inconnu"
        tp = teacher_profile_map.get(uid)
        wilaya = (p.wilaya if p else None) or (tp.teaching_wilaya if tp else None)
        entries.append({
            "user_id": uid,
            "name": name or "Inconnu",
            "wilaya": wilaya,
            "kp_level": kp_level_map.get(uid, 1),
            "kp": kp,
            "sessions": sess,
            "rating": rat,
            "score": score,
            "rank": 0,
        })

    entries.sort(key=lambda e: (-e["score"], str(e["user_id"])))
    _assign_dense_ranks(entries)
    return entries


# ─── Upsert snapshot ─────────────────────────────────────────────────────────

def _upsert_snapshot(
    db: Session,
    uid: UUID,
    period: str,
    audience: str,
    sort_by: str,
    rank: int,
    score: float,
) -> None:
    period_type, period_key = _period_key(period)
    existing = db.exec(
        select(LeaderboardRankSnapshot).where(
            LeaderboardRankSnapshot.period_type == period_type,
            LeaderboardRankSnapshot.period_key == period_key,
            LeaderboardRankSnapshot.audience == audience,
            LeaderboardRankSnapshot.sort_by == sort_by,
            LeaderboardRankSnapshot.user_id == uid,
        )
    ).first()
    if existing:
        previous_rank = existing.rank
        existing.rank = rank
        existing.score = score
        existing.created_at = datetime.now(tz=timezone.utc)
        db.add(existing)
        # Only the main/default ranking (weekly KP) triggers a notification —
        # emitting for every period_type x sort_by combination would spam the
        # user on every leaderboard recompute.
        if previous_rank != rank and period_type == "weekly" and sort_by == "kp":
            from app.services.notification_engine import emit
            emit(
                db,
                event_type="leaderboard_rank_changed",
                user_id=uid,
                context={
                    "previous_rank": previous_rank,
                    "rank": rank,
                    "direction": "amélioré" if rank < previous_rank else "reculé",
                },
                dedup_key=f"leaderboard_rank_changed:{period_key}:{audience}:{previous_rank}:{rank}",
            )
    else:
        db.add(LeaderboardRankSnapshot(
            period_type=period_type,
            period_key=period_key,
            audience=audience,
            sort_by=sort_by,
            user_id=uid,
            rank=rank,
            score=score,
        ))
    db.commit()


# ─── Endpoint ─────────────────────────────────────────────────────────────────

@router.get("/leaderboard", response_model=LeaderboardResponse)
async def get_leaderboard(
    audience: str = "students",
    period: str = "week",
    sort_by: str = "kp",
    subject: Optional[str] = None,
    level: Optional[str] = None,
    limit: int = 50,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    limit = min(limit, 100)
    me_uid = UUID(current_user["id"])

    if audience == "teachers":
        all_entries = _compute_teacher_entries(db, period, sort_by)
    else:
        all_entries = _compute_student_entries(db, period, sort_by, subject, level)

    total_ranked = len(all_entries)

    me_entry_raw: Optional[dict] = None
    me_rank: Optional[int] = None
    for e in all_entries:
        if e["user_id"] == me_uid:
            me_entry_raw = e
            me_rank = e["rank"]
            break

    top_entries = all_entries[:limit]

    def _to_response(e: dict, is_me: bool) -> LeaderboardEntry:
        return LeaderboardEntry(
            rank=e["rank"],
            user_id=str(e["user_id"]),
            name=e["name"],
            initials=_initials(e["name"]),
            wilaya=e["wilaya"],
            kp_level=e["kp_level"],
            kp=e["kp"],
            sessions=e["sessions"],
            rating=e["rating"],
            score=e["score"],
            is_me=is_me,
        )

    me_in_top = me_uid in {e["user_id"] for e in top_entries}
    entries = [_to_response(e, e["user_id"] == me_uid) for e in top_entries]

    me_response: Optional[LeaderboardEntry] = None
    if me_entry_raw and not me_in_top:
        me_response = _to_response(me_entry_raw, True)
    elif me_entry_raw and me_in_top:
        me_response = None

    try:
        if me_entry_raw:
            _upsert_snapshot(
                db, me_uid, period, audience, sort_by,
                me_entry_raw["rank"], me_entry_raw["score"],
            )
    except Exception as exc:
        logger.warning("Leaderboard snapshot upsert failed: %s", exc)

    return LeaderboardResponse(
        entries=entries,
        me=me_response,
        me_rank=me_rank,
        total_ranked=total_ranked,
        period_label=_period_label(period),
    )
