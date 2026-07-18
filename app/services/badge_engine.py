"""Badge achievement engine — compute student stats and unlock badges."""
from __future__ import annotations

from datetime import datetime, timedelta, date
from typing import Any, Dict, List, Optional, Set
from uuid import UUID

from sqlmodel import Session, func, select

from app.models.booking import TutoringSession
from app.models.gamification import Badge, UserBadge
from app.models.homework import Homework, HomeworkGrade
from app.models.kp import KpBalance
from app.models.practice import QuizAttempt
from app.models.referral import Referral


# ─── Stats dataclass ──────────────────────────────────────────────────────────

class StudentStats:
    sessions_completed: int      = 0   # real tutoring sessions with a teacher
    streak_days: int             = 0
    perfect_quizzes: int         = 0
    ep_total: int                = 0
    homework_submitted: int      = 0
    homework_high_score: int     = 0   # homeworks graded ≥ 18/20
    night_sessions: int          = 0   # real tutoring sessions scheduled after 21:00
    referrals_validated: int     = 0
    subject_hours: Dict[str, float] = None  # slug → hours

    def __init__(self) -> None:
        self.subject_hours = {}


# ─── Stat computation ─────────────────────────────────────────────────────────

def compute_student_stats(student_id: UUID, db: Session) -> StudentStats:
    stats = StudentStats()

    # ── 1. sessions_completed — REAL completed tutoring sessions only ──────────
    # Must be status='completed' in the sessions table (set by the Supabase
    # booking-completed trigger). Practice quizzes do NOT count.
    stats.sessions_completed = int(db.exec(
        select(func.count(TutoringSession.id)).where(
            TutoringSession.student_id == student_id,
            TutoringSession.status     == "completed",
        )
    ).one() or 0)

    # ── 2. perfect_quizzes (100 % score on practice quizzes) ─────────────────
    stats.perfect_quizzes = int(db.exec(
        select(func.count(QuizAttempt.id)).where(
            QuizAttempt.student_id       == student_id,
            QuizAttempt.completed        == True,   # noqa: E712
            QuizAttempt.score_percentage == 100.0,
        )
    ).one() or 0)

    # ── 3. night_sessions — real tutoring sessions scheduled after 21:00 UTC ──
    # Uses scheduled_at (the agreed session start time) from the sessions table.
    # Practice quizzes completed late do NOT count.
    night_sessions_rows = db.exec(
        select(TutoringSession.scheduled_at).where(
            TutoringSession.student_id == student_id,
            TutoringSession.status     == "completed",
            TutoringSession.scheduled_at.isnot(None),
        )
    ).all()
    stats.night_sessions = sum(1 for ts in night_sessions_rows if ts and ts.hour >= 21)

    # ── 4. ep_total (lifetime earned) ────────────────────────────────────────
    bal = db.exec(
        select(KpBalance).where(KpBalance.user_id == student_id)
    ).first()
    stats.ep_total = bal.total_earned if bal else 0

    # ── 5. homework_submitted and homework_high_score ────────────────────────
    hw_rows = db.exec(
        select(Homework.id, Homework.status).where(
            Homework.student_id == student_id,
        )
    ).all()
    submitted_ids: List[UUID] = []
    for hw_id, status in hw_rows:
        if status in ("submitted", "graded"):
            stats.homework_submitted += 1
            submitted_ids.append(hw_id)

    if submitted_ids:
        grade_rows = db.exec(
            select(HomeworkGrade.score).where(
                HomeworkGrade.homework_id.in_(submitted_ids),
                HomeworkGrade.score.isnot(None),
            )
        ).all()
        stats.homework_high_score = sum(1 for s in grade_rows if s is not None and float(s) >= 18.0)

    # ── 6. referrals_validated ────────────────────────────────────────────────
    stats.referrals_validated = int(db.exec(
        select(func.count(Referral.id)).where(
            Referral.referrer_id == student_id,
            Referral.status      == "validated",
        )
    ).one() or 0)

    # ── 7. streak_days (consecutive calendar days with any quiz activity) ─────
    attempt_dates = db.exec(
        select(QuizAttempt.completed_at).where(
            QuizAttempt.student_id == student_id,
            QuizAttempt.completed  == True,   # noqa: E712
            QuizAttempt.completed_at.isnot(None),
        ).order_by(QuizAttempt.completed_at.desc())
    ).all()

    activity_dates: Set[date] = {ts.date() for ts in attempt_dates if ts}
    stats.streak_days = _compute_streak_with_shield(activity_dates, student_id, db)

    # ── 8. subject_hours (from TutoringSession, completed sessions) ───────────
    try:
        from app.models.catalog import Subject
        sessions = db.exec(
            select(TutoringSession).where(
                TutoringSession.student_id == student_id,
                TutoringSession.status     == "completed",
                TutoringSession.subject_id.isnot(None),
            )
        ).all()
        sub_ids = {s.subject_id for s in sessions if s.subject_id}
        sub_slug_map: Dict[UUID, str] = {}
        if sub_ids:
            subs = db.exec(select(Subject).where(Subject.id.in_(sub_ids))).all()
            sub_slug_map = {s.id: s.slug for s in subs}

        for sess in sessions:
            slug = sub_slug_map.get(sess.subject_id)
            if not slug:
                continue
            duration_h = _session_hours(sess)
            stats.subject_hours[slug] = stats.subject_hours.get(slug, 0.0) + duration_h
    except Exception:
        pass

    return stats


def _compute_streak(activity_dates: Set[date]) -> int:
    """Return consecutive calendar days ending on or before today."""
    if not activity_dates:
        return 0
    today = datetime.utcnow().date()
    streak = 0
    check = today
    while check in activity_dates:
        streak += 1
        check -= timedelta(days=1)
    if streak == 0:
        yesterday = today - timedelta(days=1)
        check = yesterday
        while check in activity_dates:
            streak += 1
            check -= timedelta(days=1)
    return streak


def _compute_streak_with_shield(activity_dates: Set[date], student_id, db) -> int:
    """Like _compute_streak but consumes an active streak_shield when it would save the streak."""
    from uuid import UUID as _UUID
    today = datetime.utcnow().date()
    yesterday = today - timedelta(days=1)
    two_days_ago = today - timedelta(days=2)

    dates = set(activity_dates)

    # Shield fires when yesterday is missing AND two_days_ago has activity
    if yesterday not in dates and two_days_ago in dates:
        try:
            uid = _UUID(str(student_id)) if not isinstance(student_id, _UUID) else student_id
            from app.services.effects import get_active_streak_shield, consume_streak_shield
            shield = get_active_streak_shield(uid, db)
            if shield:
                consume_streak_shield(shield, db)
                db.commit()
                dates = dates | {yesterday}
        except Exception:
            pass

    streak = 0
    check = today
    while check in dates:
        streak += 1
        check -= timedelta(days=1)
    if streak == 0:
        check = yesterday
        while check in dates:
            streak += 1
            check -= timedelta(days=1)
    return streak


def _session_hours(sess: Any) -> float:
    """Estimate session duration in hours. TutoringSession uses duration_min."""
    try:
        minutes = getattr(sess, "duration_min", None)
        if minutes and isinstance(minutes, (int, float)):
            return float(minutes) / 60.0
    except Exception:
        pass
    return 1.0  # default 1h per session


# ─── Badge unlock check ───────────────────────────────────────────────────────

def check_and_unlock_badges(
    student_id: UUID,
    db: Session,
    stats: Optional[StudentStats] = None,
) -> List[str]:
    """
    Evaluate all active badge conditions for this student.
    Inserts new `user_badges` rows for each newly unlocked badge.
    Returns list of newly unlocked badge IDs (for immediate UI feedback).
    """
    if stats is None:
        stats = compute_student_stats(student_id, db)

    # Already-unlocked badge IDs
    existing = db.exec(
        select(UserBadge.badge_id).where(UserBadge.user_id == student_id)
    ).all()
    already_unlocked: Set[str] = set(existing)

    # All active badges with condition logic (student-facing only — migration
    # 058 added teacher-only trophies scoped out via `audience`).
    badges = db.exec(
        select(Badge).where(
            Badge.active == True,   # noqa: E712
            Badge.condition_type.isnot(None),
            Badge.audience.in_(["student", "both"]),
        )
    ).all()

    newly_unlocked: List[str] = []

    for badge in badges:
        if badge.id in already_unlocked:
            continue

        threshold = badge.condition_threshold or 0
        met = False

        ct = badge.condition_type
        if ct == "sessions_completed":
            met = stats.sessions_completed >= threshold
        elif ct == "streak_days":
            met = stats.streak_days >= threshold
        elif ct == "perfect_quizzes":
            met = stats.perfect_quizzes >= threshold
        elif ct == "ep_total":
            met = stats.ep_total >= threshold
        elif ct == "homework_submitted":
            met = stats.homework_submitted >= threshold
        elif ct == "homework_high_score":
            met = stats.homework_high_score >= threshold
        elif ct == "night_sessions":
            met = stats.night_sessions >= threshold
        elif ct == "referrals_validated":
            met = stats.referrals_validated >= threshold
        elif ct == "subject_hours":
            slug = badge.condition_subject_slug or ""
            hours = stats.subject_hours.get(slug, 0.0)
            met = hours >= threshold

        if met:
            db.add(UserBadge(
                user_id=student_id,
                badge_id=badge.id,
                unlocked_at=datetime.utcnow(),
                progress_current=threshold,
                progress_total=threshold,
                viewed_at=None,  # NULL = show animation
            ))
            newly_unlocked.append(badge.id)

    if newly_unlocked:
        db.commit()

        from app.services.notification_engine import emit
        badges_by_id = {b.id: b for b in badges}
        for badge_id in newly_unlocked:
            badge = badges_by_id.get(badge_id)
            emit(
                db, event_type="badge_unlocked", user_id=student_id,
                context={"badge_name": badge.name if badge else "nouveau badge"},
                data={"badge_id": badge_id},
                dedup_key=f"badge_unlocked:{student_id}:{badge_id}",
            )

    return newly_unlocked


# ─── Progress helper (for badge progress bars on locked badges) ───────────────

def badge_progress(badge: Badge, stats: StudentStats) -> tuple[int, int]:
    """Return (current, total) progress values for a locked badge."""
    threshold = badge.condition_threshold or 1
    ct = badge.condition_type

    if ct == "sessions_completed":
        cur = min(stats.sessions_completed, threshold)
    elif ct == "streak_days":
        cur = min(stats.streak_days, threshold)
    elif ct == "perfect_quizzes":
        cur = min(stats.perfect_quizzes, threshold)
    elif ct == "ep_total":
        cur = min(stats.ep_total, threshold)
    elif ct == "homework_submitted":
        cur = min(stats.homework_submitted, threshold)
    elif ct == "homework_high_score":
        cur = min(stats.homework_high_score, threshold)
    elif ct == "night_sessions":
        cur = min(stats.night_sessions, threshold)
    elif ct == "referrals_validated":
        cur = min(stats.referrals_validated, threshold)
    elif ct == "subject_hours":
        slug = badge.condition_subject_slug or ""
        cur = min(int(stats.subject_hours.get(slug, 0.0)), threshold)
    else:
        cur = 0

    return cur, threshold
