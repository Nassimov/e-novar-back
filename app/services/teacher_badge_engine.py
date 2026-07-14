"""Trophy achievement engine for teachers — compute teacher stats and unlock trophies.

Mirrors app/services/badge_engine.py (the student badge engine) exactly, but
grounds every condition in teacher-side data: completed tutoring sessions
given, distinct students taught, hours taught, review ratings and account
tenure. Reuses the same `badges` / `user_badges` tables (migration 058 added
an `audience` column to `badges` so teacher trophies don't leak into the
student badges page and vice versa).
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Set
from uuid import UUID

from sqlmodel import Session, func, select

from app.models.booking import TutoringSession
from app.models.gamification import Badge, UserBadge
from app.models.profile import Profile
from app.models.referral import Referral
from app.models.review import Review


# ─── Stats dataclass ──────────────────────────────────────────────────────────

class TeacherStats:
    sessions_completed: int      = 0   # completed tutoring sessions given by this teacher
    students_taught: int         = 0   # distinct students across completed sessions
    repeat_students: int         = 0   # distinct students with >= 2 completed sessions (loyalty)
    hours_taught: float          = 0.0 # sum of duration_min / 60 across completed sessions
    five_star_reviews: int       = 0   # visible reviews with rating == 5
    perfect_rating_streak: int   = 0   # consecutive most-recent visible reviews rated 5
    reviews_count: int           = 0   # total visible reviews received
    rating_avg: float            = 0.0 # average of visible review ratings
    tenure_days: int             = 0   # days since profile creation
    referrals_validated: int     = 0   # validated referrals where this teacher is the referrer


# ─── Stat computation ─────────────────────────────────────────────────────────

def compute_teacher_stats(teacher_id: UUID, db: Session) -> TeacherStats:
    stats = TeacherStats()

    # ── 1. sessions_completed — REAL completed tutoring sessions given ───────
    stats.sessions_completed = int(db.exec(
        select(func.count(TutoringSession.id)).where(
            TutoringSession.teacher_id == teacher_id,
            TutoringSession.status     == "completed",
        )
    ).one() or 0)

    # ── 2. students_taught / repeat_students — grouped by distinct student ──
    student_counts = db.exec(
        select(TutoringSession.student_id, func.count(TutoringSession.id)).where(
            TutoringSession.teacher_id == teacher_id,
            TutoringSession.status     == "completed",
        ).group_by(TutoringSession.student_id)
    ).all()
    stats.students_taught = len(student_counts)
    stats.repeat_students = sum(1 for _sid, cnt in student_counts if cnt >= 2)

    # ── 3. hours_taught — sum of session durations (default 90 min if unset) ─
    duration_rows = db.exec(
        select(TutoringSession.duration_min).where(
            TutoringSession.teacher_id == teacher_id,
            TutoringSession.status     == "completed",
        )
    ).all()
    stats.hours_taught = sum((d if d else 90) / 60.0 for d in duration_rows)

    # ── 4. review-based stats (visible reviews only) ─────────────────────────
    review_ratings = db.exec(
        select(Review.rating).where(
            Review.teacher_id == teacher_id,
            Review.status     == "visible",
        ).order_by(Review.created_at.desc())
    ).all()
    stats.reviews_count = len(review_ratings)
    stats.five_star_reviews = sum(1 for r in review_ratings if r == 5)
    stats.rating_avg = round(sum(review_ratings) / len(review_ratings), 2) if review_ratings else 0.0

    streak = 0
    for r in review_ratings:  # already ordered most-recent first
        if r == 5:
            streak += 1
        else:
            break
    stats.perfect_rating_streak = streak

    # ── 5. tenure_days — account age from profiles.created_at ────────────────
    profile = db.get(Profile, teacher_id)
    if profile and profile.created_at:
        stats.tenure_days = max(0, (datetime.utcnow() - profile.created_at).days)

    # ── 6. referrals_validated — teacher as referrer (role-agnostic table) ───
    stats.referrals_validated = int(db.exec(
        select(func.count(Referral.id)).where(
            Referral.referrer_id == teacher_id,
            Referral.status      == "validated",
        )
    ).one() or 0)

    return stats


# ─── Trophy unlock check ──────────────────────────────────────────────────────

def check_and_unlock_teacher_badges(
    teacher_id: UUID,
    db: Session,
    stats: Optional[TeacherStats] = None,
) -> List[str]:
    """
    Evaluate all active teacher-audience badge conditions for this teacher.
    Inserts new `user_badges` rows for each newly unlocked trophy.
    Returns list of newly unlocked badge IDs (for immediate UI feedback).
    """
    if stats is None:
        stats = compute_teacher_stats(teacher_id, db)

    existing = db.exec(
        select(UserBadge.badge_id).where(UserBadge.user_id == teacher_id)
    ).all()
    already_unlocked: Set[str] = set(existing)

    badges = db.exec(
        select(Badge).where(
            Badge.active == True,   # noqa: E712
            Badge.condition_type.isnot(None),
            Badge.audience.in_(["teacher", "both"]),
        )
    ).all()

    newly_unlocked: List[str] = []

    for badge in badges:
        if badge.id in already_unlocked:
            continue

        threshold = badge.condition_threshold or 0
        met = False

        ct = badge.condition_type
        if ct == "teacher_sessions_completed":
            met = stats.sessions_completed >= threshold
        elif ct == "teacher_students_taught":
            met = stats.students_taught >= threshold
        elif ct == "teacher_hours_taught":
            met = stats.hours_taught >= threshold
        elif ct == "teacher_five_star_reviews":
            met = stats.five_star_reviews >= threshold
        elif ct == "teacher_perfect_rating_streak":
            met = stats.perfect_rating_streak >= threshold
        elif ct == "teacher_tenure_days":
            met = stats.tenure_days >= threshold
        elif ct == "teacher_referrals_validated":
            met = stats.referrals_validated >= threshold

        if met:
            db.add(UserBadge(
                user_id=teacher_id,
                badge_id=badge.id,
                unlocked_at=datetime.utcnow(),
                progress_current=threshold,
                progress_total=threshold,
                viewed_at=None,  # NULL = show animation
            ))
            newly_unlocked.append(badge.id)

    if newly_unlocked:
        db.commit()

    return newly_unlocked


# ─── Progress helper (for progress bars on locked trophies) ──────────────────

def teacher_badge_progress(badge: Badge, stats: TeacherStats) -> tuple[int, int]:
    """Return (current, total) progress values for a locked trophy."""
    threshold = badge.condition_threshold or 1
    ct = badge.condition_type

    if ct == "teacher_sessions_completed":
        cur = min(stats.sessions_completed, threshold)
    elif ct == "teacher_students_taught":
        cur = min(stats.students_taught, threshold)
    elif ct == "teacher_hours_taught":
        cur = min(int(stats.hours_taught), threshold)
    elif ct == "teacher_five_star_reviews":
        cur = min(stats.five_star_reviews, threshold)
    elif ct == "teacher_perfect_rating_streak":
        cur = min(stats.perfect_rating_streak, threshold)
    elif ct == "teacher_tenure_days":
        cur = min(stats.tenure_days, threshold)
    elif ct == "teacher_referrals_validated":
        cur = min(stats.referrals_validated, threshold)
    else:
        cur = 0

    return cur, threshold
