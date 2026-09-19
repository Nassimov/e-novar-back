from __future__ import annotations

"""
GET /api/student/teachers/{ref}/busy-times (2026-09-19 request): the
student-facing "open hour" (no declared TeacherSlot) proposal picker was
offering times the teacher was actually already teaching someone at —
get_teacher_slots only ever reports booked_ranges tied to a specific
slot_id, so a slot-less custom booking (or a booking against a DIFFERENT
slot) was invisible to it entirely. This endpoint is the fix: it reports
every CONFIRMED booking's actual scheduled time regardless of slot origin,
same status/confirmed filter as matching.find_overlapping_confirmed_sessions
(the single source of truth book_teacher_slot itself uses to reject a
conflicting booking).

Real router function called directly as plain Python — same convention as
tests/test_classroom_session_workflow.py.
"""

import datetime as dt
from uuid import uuid4

from app.models.booking import Booking, TutoringSession
from app.models.profile import Profile, TeacherProfile
from app.routers import student_teachers as routes


def _make_profile(db, **overrides):
    fields = {"id": uuid4(), "email": f"{uuid4()}@example.com", "first_name": "Test", "last_name": "User"}
    fields.update(overrides)
    p = Profile(**fields)
    db.add(p)
    db.commit()
    return p


def _make_teacher(db):
    profile = _make_profile(db)
    tp = TeacherProfile(user_id=profile.id, status="approved", verified=True)
    db.add(tp)
    db.commit()
    return profile, tp


def _make_session(db, *, teacher_id, student_id, booking_status, session_status, scheduled_at, slot_id=None):
    booking = Booking(
        student_id=student_id, teacher_id=teacher_id, slot_id=slot_id,
        booking_date=scheduled_at.date(), status=booking_status, duration_min=60,
    )
    db.add(booking)
    db.commit()
    session = TutoringSession(
        booking_id=booking.id, teacher_id=teacher_id, student_id=student_id,
        scheduled_at=scheduled_at, duration_min=60, status=session_status,
    )
    db.add(session)
    db.commit()
    return booking, session


def _current_user(profile):
    return {"id": str(profile.id), "email": profile.email, "role": "student", "claims": {}}


def test_confirmed_slot_less_booking_shows_as_busy(db_session):
    teacher, _tp = _make_teacher(db_session)
    student = _make_profile(db_session)
    tomorrow = dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=1)
    scheduled_at = dt.datetime.combine(tomorrow, dt.time(15, 0), tzinfo=dt.timezone.utc)
    _make_session(
        db_session, teacher_id=teacher.id, student_id=student.id,
        booking_status="confirmed", session_status="scheduled", scheduled_at=scheduled_at,
    )

    result = routes.get_teacher_busy_times(str(teacher.id), db_session, _current_user(student))

    assert len(result) == 1
    assert result[0].date == tomorrow.isoformat()
    assert result[0].start_time == "15:00"
    assert result[0].end_time == "16:00"


def test_pending_booking_does_not_show_as_busy(db_session):
    teacher, _tp = _make_teacher(db_session)
    student = _make_profile(db_session)
    tomorrow = dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=1)
    scheduled_at = dt.datetime.combine(tomorrow, dt.time(10, 0), tzinfo=dt.timezone.utc)
    _make_session(
        db_session, teacher_id=teacher.id, student_id=student.id,
        booking_status="pending", session_status="scheduled", scheduled_at=scheduled_at,
    )

    result = routes.get_teacher_busy_times(str(teacher.id), db_session, _current_user(student))
    assert result == []


def test_cancelled_session_does_not_show_as_busy(db_session):
    teacher, _tp = _make_teacher(db_session)
    student = _make_profile(db_session)
    tomorrow = dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=1)
    scheduled_at = dt.datetime.combine(tomorrow, dt.time(11, 0), tzinfo=dt.timezone.utc)
    _make_session(
        db_session, teacher_id=teacher.id, student_id=student.id,
        booking_status="confirmed", session_status="cancelled", scheduled_at=scheduled_at,
    )

    result = routes.get_teacher_busy_times(str(teacher.id), db_session, _current_user(student))
    assert result == []


def test_same_student_mid_reservation_also_shows_as_busy(db_session):
    """The bug report explicitly named this case: the SAME student who
    already has a confirmed session must not be shown that time again as
    'open' either — busy-times has no student filter, it's purely about
    the teacher's own schedule."""
    teacher, _tp = _make_teacher(db_session)
    student = _make_profile(db_session)
    tomorrow = dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=1)
    scheduled_at = dt.datetime.combine(tomorrow, dt.time(9, 0), tzinfo=dt.timezone.utc)
    _make_session(
        db_session, teacher_id=teacher.id, student_id=student.id,
        booking_status="confirmed", session_status="scheduled", scheduled_at=scheduled_at,
    )

    another_student = _make_profile(db_session)
    result = routes.get_teacher_busy_times(str(teacher.id), db_session, _current_user(another_student))
    assert len(result) == 1
    assert result[0].start_time == "09:00"


def test_busy_times_outside_30_day_window_excluded(db_session):
    teacher, _tp = _make_teacher(db_session)
    student = _make_profile(db_session)
    far_future = dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=60)
    scheduled_at = dt.datetime.combine(far_future, dt.time(9, 0), tzinfo=dt.timezone.utc)
    _make_session(
        db_session, teacher_id=teacher.id, student_id=student.id,
        booking_status="confirmed", session_status="scheduled", scheduled_at=scheduled_at,
    )

    result = routes.get_teacher_busy_times(str(teacher.id), db_session, _current_user(student))
    assert result == []
