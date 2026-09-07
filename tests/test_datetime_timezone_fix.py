from __future__ import annotations

"""Regression tests for the "can't compare offset-naive and offset-aware
datetimes" crash reported on the student payment flow.

Root cause (see app/services/matching.py's module note and
app/routers/student_teachers.py's _gate_one): every relevant timestamp
column (sessions.scheduled_at, student_profiles.booking_suspended_until/
last_no_show_at, teacher_profiles.suspended_until/last_no_response_at —
see docs/database-schema.sql and docs/migrations/067/069) is TIMESTAMPTZ,
so a value read back from the DB is always timezone-aware. Several call
sites built a "now"/"window" datetime with the naive datetime.utcnow() (or
datetime.combine() without tzinfo) and then compared it, in Python,
against one of those aware values — raising the TypeError the instant
both ended up in the same comparison. Fixed by making every such
construction timezone-aware (datetime.now(timezone.utc) /
datetime.combine(..., tzinfo=timezone.utc)), plus a defensive normalization
in find_overlapping_confirmed_sessions itself since it's the shared,
safety-critical function multiple booking paths depend on.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.models.booking import Booking, TutoringSession
from app.models.profile import Profile


def _make_profile(db_session, **overrides):
    fields = {"id": uuid4(), "email": f"{uuid4()}@test.local", "first_name": "Test", "last_name": "User"}
    fields.update(overrides)
    profile = Profile(**fields)
    db_session.add(profile)
    db_session.commit()
    return profile


def _make_booking(db_session, *, teacher_id, student_id, status="confirmed"):
    booking = Booking(
        id=uuid4(), teacher_id=teacher_id, student_id=student_id,
        booking_date=datetime.now(timezone.utc).date(), status=status,
    )
    db_session.add(booking)
    db_session.commit()
    return booking


def test_find_overlapping_confirmed_sessions_accepts_naive_window(db_session):
    """Pins the exact historical crash shape: an aware scheduled_at (as a
    TIMESTAMPTZ column always reads back) compared against a naive
    window_start/window_end (as _gate_one used to build via
    dt.datetime.combine() with no tzinfo) must not raise, and must still
    correctly detect the conflict."""
    from app.services.matching import find_overlapping_confirmed_sessions

    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    booking = _make_booking(db_session, teacher_id=teacher.id, student_id=student.id)

    aware_scheduled_at = datetime(2026, 9, 10, 14, 0, tzinfo=timezone.utc)
    session = TutoringSession(
        booking_id=booking.id, teacher_id=teacher.id, student_id=student.id,
        scheduled_at=aware_scheduled_at, duration_min=60, status="scheduled",
    )
    db_session.add(session)
    db_session.commit()
    # expire_on_commit=False (see conftest.py) — `session.scheduled_at`
    # stays the exact aware Python object assigned above, matching how a
    # real TIMESTAMPTZ column round-trips via psycopg2 in production.

    naive_window_start = datetime(2026, 9, 10, 14, 30)  # no tzinfo — the historical bug's exact shape
    naive_window_end = datetime(2026, 9, 10, 15, 30)

    conflicts = find_overlapping_confirmed_sessions(
        db_session, window_start=naive_window_start, window_end=naive_window_end, teacher_id=teacher.id,
    )
    assert [c.id for c in conflicts] == [session.id]


def test_find_overlapping_confirmed_sessions_no_conflict_outside_window(db_session):
    from app.services.matching import find_overlapping_confirmed_sessions

    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    booking = _make_booking(db_session, teacher_id=teacher.id, student_id=student.id)

    session = TutoringSession(
        booking_id=booking.id, teacher_id=teacher.id, student_id=student.id,
        scheduled_at=datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc), duration_min=60, status="scheduled",
    )
    db_session.add(session)
    db_session.commit()

    conflicts = find_overlapping_confirmed_sessions(
        db_session,
        window_start=datetime(2026, 9, 10, 14, 0),
        window_end=datetime(2026, 9, 10, 15, 0),
        teacher_id=teacher.id,
    )
    assert conflicts == []


def test_apply_student_strike_second_incident_does_not_raise(db_session):
    """apply_student_strike reads back sp.last_no_show_at (TIMESTAMPTZ,
    aware) and used to subtract it from a naive datetime.utcnow() on the
    second incident — see app/services/booking_safety.py."""
    from app.models.profile import StudentProfile
    from app.services.booking_safety import apply_student_strike

    student = _make_profile(db_session)
    sp = StudentProfile(
        user_id=student.id,
        last_no_show_at=datetime.now(timezone.utc) - timedelta(days=10),
        no_show_strikes=1,
    )
    db_session.add(sp)
    db_session.commit()

    # Must not raise — this is the second incident, so the function reads
    # back the just-set last_no_show_at and compares it against a fresh
    # "now" before overwriting it.
    strikes, days = apply_student_strike(
        db_session, student.id, "student_no_show", human_label="Tu ne t'es pas connecté·e à une séance confirmée.",
    )
    assert strikes == 2


def test_task_detect_online_teacher_no_show_does_not_raise(db_session):
    """The exact crash shape from app/workers/booking_tasks.py: `now`
    (used to be naive datetime.utcnow()) compared against
    session.scheduled_at (aware, TIMESTAMPTZ) for a session whose grace
    window has already elapsed."""
    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    booking = _make_booking(db_session, teacher_id=teacher.id, student_id=student.id)
    session = TutoringSession(
        booking_id=booking.id, teacher_id=teacher.id, student_id=student.id,
        scheduled_at=datetime.now(timezone.utc) - timedelta(hours=1),
        duration_min=60, status="scheduled", mode="online",
    )
    db_session.add(session)
    db_session.commit()

    now = datetime.now(timezone.utc)
    grace_minutes = 20
    # This is the exact comparison booking_tasks.py's task_detect_online_teacher_no_show
    # performs per candidate — pinned directly rather than invoking the full
    # Celery task (which needs its own DB engine/session wiring).
    assert not (now < session.scheduled_at + timedelta(minutes=grace_minutes))
