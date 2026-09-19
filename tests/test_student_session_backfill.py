from __future__ import annotations

"""
Self-heal for a real data gap found via direct production inspection
(2026-09-19 report): "student books, teacher accepts and can enter the
session, but the student sees it in neither the pending nor upcoming tab."

Root cause: teachers.py's list_teacher_bookings reads straight from the
Booking table (always shows a confirmed booking regardless of whether any
TutoringSession exists), while student_dashboard.py's student_session_list
and student_dashboard read exclusively from TutoringSession — a confirmed
booking with zero session rows is therefore completely invisible to the
student even though the teacher sees and can act on it. Direct prod query
confirmed at least one such orphaned booking exists (a confirmed cash pack5
booking with 0 TutoringSession rows).

app.routers.student_dashboard._backfill_missing_tutoring_sessions
reconstructs the missing session(s) from the booking's own stored data —
this file exercises it directly, plus through the real student_session_list
and student_dashboard endpoints, same convention as
tests/test_classroom_session_workflow.py (real router functions, in-memory
SQLite).
"""

import datetime as dt
import json
from uuid import uuid4

from app.models.booking import Booking, TutoringSession
from app.models.profile import Profile
from app.models.session_validation import SessionValidation
from app.routers import student_dashboard as routes


def _make_profile(db, **overrides):
    fields = {"id": uuid4(), "email": f"{uuid4()}@example.com", "first_name": "Test", "last_name": "User"}
    fields.update(overrides)
    p = Profile(**fields)
    db.add(p)
    db.commit()
    return p


def _current_user(profile):
    return {"id": str(profile.id), "email": profile.email, "role": "student", "claims": {}}


def test_backfill_reconstructs_single_booking_with_no_session(db_session):
    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    tomorrow = dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=1)
    booking = Booking(
        student_id=student.id, teacher_id=teacher.id, formula="single", mode="online",
        booking_date=tomorrow, slot_time=dt.time(14, 0), duration_min=60,
        status="confirmed", payment_method="cash",
    )
    db_session.add(booking)
    db_session.commit()

    assert db_session.exec(
        __import__("sqlmodel").select(TutoringSession).where(TutoringSession.booking_id == booking.id)
    ).all() == []

    routes._backfill_missing_tutoring_sessions(db_session, student.id)

    sessions = db_session.exec(
        __import__("sqlmodel").select(TutoringSession).where(TutoringSession.booking_id == booking.id)
    ).all()
    assert len(sessions) == 1
    s = sessions[0]
    assert s.student_id == student.id
    assert s.teacher_id == teacher.id
    assert s.status == "scheduled"
    assert s.duration_min == 60
    # SQLite doesn't round-trip tz-aware datetimes (see conftest.py) — real
    # Postgres TIMESTAMPTZ always comes back aware; compare naive here.
    assert s.scheduled_at.replace(tzinfo=None) == dt.datetime.combine(tomorrow, dt.time(14, 0))

    validations = db_session.exec(
        __import__("sqlmodel").select(SessionValidation).where(SessionValidation.session_id == s.id)
    ).all()
    assert len(validations) == 1
    assert validations[0].student_id == student.id


def test_backfill_reconstructs_pack5_booking_from_pack_sessions_json(db_session):
    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    tomorrow = dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=1)
    legs = [
        {"date": tomorrow.isoformat(), "slot_time": f"{h:02d}:00", "end_time": f"{h + 1:02d}:00", "session_type": "individual"}
        for h in range(10, 15)
    ]
    booking = Booking(
        student_id=student.id, teacher_id=teacher.id, formula="pack5", mode="online",
        booking_date=tomorrow, slot_time=dt.time(10, 0), duration_min=60,
        status="confirmed", payment_method="cash", pack_sessions=json.dumps(legs),
    )
    db_session.add(booking)
    db_session.commit()

    routes._backfill_missing_tutoring_sessions(db_session, student.id)

    sessions = db_session.exec(
        __import__("sqlmodel").select(TutoringSession).where(TutoringSession.booking_id == booking.id)
    ).all()
    assert len(sessions) == 5
    assert {s.duration_min for s in sessions} == {60}
    assert sorted(s.scheduled_at.hour for s in sessions) == [10, 11, 12, 13, 14]


def test_backfill_is_idempotent(db_session):
    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    tomorrow = dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=1)
    booking = Booking(
        student_id=student.id, teacher_id=teacher.id, formula="single", mode="online",
        booking_date=tomorrow, slot_time=dt.time(9, 0), duration_min=60,
        status="confirmed", payment_method="cash",
    )
    db_session.add(booking)
    db_session.commit()

    routes._backfill_missing_tutoring_sessions(db_session, student.id)
    routes._backfill_missing_tutoring_sessions(db_session, student.id)

    sessions = db_session.exec(
        __import__("sqlmodel").select(TutoringSession).where(TutoringSession.booking_id == booking.id)
    ).all()
    assert len(sessions) == 1


def test_backfill_ignores_pending_bookings(db_session):
    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    tomorrow = dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=1)
    booking = Booking(
        student_id=student.id, teacher_id=teacher.id, formula="single", mode="online",
        booking_date=tomorrow, slot_time=dt.time(9, 0), duration_min=60,
        status="pending", payment_method="cash",
    )
    db_session.add(booking)
    db_session.commit()

    routes._backfill_missing_tutoring_sessions(db_session, student.id)

    sessions = db_session.exec(
        __import__("sqlmodel").select(TutoringSession).where(TutoringSession.booking_id == booking.id)
    ).all()
    assert sessions == []


def test_student_session_list_shows_previously_invisible_confirmed_booking(db_session):
    """End-to-end through the real endpoint the student's app actually
    calls (studentSessionsApi.list) — this is exactly the reported bug."""
    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    tomorrow = dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=1)
    booking = Booking(
        student_id=student.id, teacher_id=teacher.id, formula="single", mode="online",
        booking_date=tomorrow, slot_time=dt.time(16, 0), duration_min=60,
        status="confirmed", payment_method="cash",
    )
    db_session.add(booking)
    db_session.commit()

    result = routes.student_session_list(
        type="upcoming", page=1, size=20, current_user=_current_user(student), db=db_session,
    )
    assert result.total == 1
    assert result.items[0].booking_status == "confirmed"
