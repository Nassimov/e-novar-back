from __future__ import annotations

"""
Teacher <-> student video call workflow — before (waiting room)/during
(live call)/after (session validation) — focused, real automated tests.

Written after a production bug was found and fixed: TutoringSession.
teacher_joined_at/student_joined_at (the fields the online no-show
detector in app/workers/booking_tasks.py relies on) used to be set by
GET /{session_id}/room — a mere room-info fetch the waiting-room page
polls every 15s, well before anyone actually joins the LiveKit call. They
are now set by POST /{session_id}/validation/online-connect instead,
which only fires from the live page's LiveKitRoom `onConnected` callback
— i.e. a real WebRTC connection. These tests pin that behavior down so it
can't silently regress.
"""

import datetime as dt
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.booking import Booking, TutoringSession
from app.models.profile import Profile


def _make_profile(db_session, **overrides):
    # Profile has no `role` column — role is a JWT (app_metadata) concept
    # for real users, so tests track it separately (see _current_user).
    fields = {"id": uuid4(), "email": f"{uuid4()}@test.local", "first_name": "Test", "last_name": "User"}
    fields.update(overrides)
    profile = Profile(**fields)
    db_session.add(profile)
    db_session.commit()
    return profile


def _make_session(db_session, *, teacher_id, student_id, scheduled_at, **overrides):
    fields = {
        "id": uuid4(), "teacher_id": teacher_id, "student_id": student_id,
        "scheduled_at": scheduled_at, "duration_min": 60, "mode": "online", "status": "scheduled",
    }
    fields.update(overrides)
    session = TutoringSession(**fields)
    db_session.add(session)
    db_session.commit()
    # No .refresh() here — expire_on_commit=False (see conftest.py) means
    # the object already keeps its Python-set values (including tz-aware
    # scheduled_at, which SQLite would otherwise strip on reload).
    return session


def _make_booking(db_session, *, teacher_id, student_id, session_type="individual", slot_id=None, status="confirmed"):
    booking = Booking(
        id=uuid4(), teacher_id=teacher_id, student_id=student_id, session_type=session_type,
        slot_id=slot_id, booking_date=dt.date.today(), status=status,
    )
    db_session.add(booking)
    db_session.commit()
    return booking


def _current_user(profile: Profile, role: str = "student") -> dict:
    return {"id": str(profile.id), "email": profile.email, "role": role}


# ─── Room access authorization ──────────────────────────────────────────────

def test_room_access_denied_for_non_participant(db_session):
    from app.routers.classroom import _authorize

    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    stranger = _make_profile(db_session)
    session = _make_session(db_session, teacher_id=teacher.id, student_id=student.id, scheduled_at=datetime.now(timezone.utc))

    with pytest.raises(HTTPException) as exc_info:
        _authorize(session, _current_user(stranger))
    assert exc_info.value.status_code == 403


def test_room_access_allowed_for_participant_and_admin(db_session):
    from app.routers.classroom import _authorize

    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    admin = _make_profile(db_session)
    session = _make_session(db_session, teacher_id=teacher.id, student_id=student.id, scheduled_at=datetime.now(timezone.utc))

    assert _authorize(session, _current_user(teacher, role="teacher")) == teacher.id
    assert _authorize(session, _current_user(student)) == student.id
    assert _authorize(session, _current_user(admin, role="admin")) == admin.id  # admin bypass, never a participant


@pytest.mark.asyncio
async def test_room_rejects_non_online_mode(db_session):
    from app.routers.classroom import get_classroom_room

    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    session = _make_session(db_session, teacher_id=teacher.id, student_id=student.id, scheduled_at=datetime.now(timezone.utc), mode="at_home")

    with pytest.raises(HTTPException) as exc_info:
        await get_classroom_room(session.id, _current_user(teacher, role="teacher"), db_session)
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_room_rejects_cancelled_session(db_session):
    from app.routers.classroom import get_classroom_room

    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    session = _make_session(db_session, teacher_id=teacher.id, student_id=student.id, scheduled_at=datetime.now(timezone.utc), status="cancelled")

    with pytest.raises(HTTPException) as exc_info:
        await get_classroom_room(session.id, _current_user(teacher, role="teacher"), db_session)
    assert exc_info.value.status_code == 409


# ─── Join-window gate + the join-timestamp regression fix ─────────────────

@pytest.mark.asyncio
async def test_room_before_join_window_returns_cannot_join_without_mutating_anything(db_session):
    """Well before the window opens: no LiveKit call is ever made (would
    hang/fail without network access in a test), can_join=False, and
    nothing about the session is touched."""
    from app.routers.classroom import get_classroom_room

    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    far_future = datetime.now(timezone.utc) + timedelta(days=3)
    session = _make_session(db_session, teacher_id=teacher.id, student_id=student.id, scheduled_at=far_future)

    result = await get_classroom_room(session.id, _current_user(student), db_session)

    assert result.can_join is False
    assert result.token == ""
    db_session.refresh(session)
    assert session.status == "scheduled"
    assert session.teacher_joined_at is None
    assert session.student_joined_at is None


@pytest.mark.asyncio
async def test_room_within_join_window_flips_live_but_never_sets_join_timestamps(db_session):
    """THE regression test: merely fetching room/token info (what the
    waiting-room page's 15s poll does) must flip status to 'live' for
    display purposes, but must NEVER set teacher_joined_at/student_joined_at
    — those are reserved for an actual LiveKit connection (see
    test_record_online_connect_* below)."""
    from app.routers.classroom import get_classroom_room

    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    session = _make_session(db_session, teacher_id=teacher.id, student_id=student.id, scheduled_at=datetime.now(timezone.utc))

    with patch("app.services.livekit_video.get_or_create_room", new=AsyncMock(return_value={"name": "session-x", "sid": "RM_x"})), \
         patch("app.services.livekit_video.create_access_token", return_value="fake.jwt.token"):
        result = await get_classroom_room(session.id, _current_user(student), db_session)

    assert result.can_join is True
    assert result.token == "fake.jwt.token"
    db_session.refresh(session)
    assert session.status == "live"
    assert session.started_at is not None
    assert session.teacher_joined_at is None  # <- the bug: this used to be set here
    assert session.student_joined_at is None  # <- the bug: this used to be set here


# ─── record_online_connect: the REAL join signal ───────────────────────────

@pytest.mark.asyncio
async def test_record_online_connect_sets_join_timestamp_for_caller_only(db_session):
    from app.routers.session_validation import record_online_connect

    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    session = _make_session(db_session, teacher_id=teacher.id, student_id=student.id, scheduled_at=datetime.now(timezone.utc))

    await record_online_connect(session.id, _current_user(student), db_session)

    db_session.refresh(session)
    assert session.student_joined_at is not None
    assert session.teacher_joined_at is None  # teacher hasn't connected — must stay untouched


@pytest.mark.asyncio
async def test_record_online_connect_is_idempotent(db_session):
    from app.routers.session_validation import record_online_connect

    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    session = _make_session(db_session, teacher_id=teacher.id, student_id=student.id, scheduled_at=datetime.now(timezone.utc))

    await record_online_connect(session.id, _current_user(student), db_session)
    db_session.refresh(session)
    first_ts = session.student_joined_at

    await record_online_connect(session.id, _current_user(student), db_session)
    db_session.refresh(session)
    assert session.student_joined_at == first_ts  # never overwritten by a second call


@pytest.mark.asyncio
async def test_record_online_connect_group_lesson_marks_teacher_on_every_sibling(db_session):
    """A group lesson has one TutoringSession row per enrolled student, all
    sharing the teacher — the teacher connecting via ANY one of them must
    mark teacher_joined_at on ALL sibling rows, or the no-show detector
    would wrongly flag the teacher absent on the others."""
    from app.routers.session_validation import record_online_connect

    teacher = _make_profile(db_session)
    student_a = _make_profile(db_session)
    student_b = _make_profile(db_session)
    slot_id = uuid4()
    now = datetime.now(timezone.utc)

    booking_a = _make_booking(db_session, teacher_id=teacher.id, student_id=student_a.id, session_type="group", slot_id=slot_id)
    booking_b = _make_booking(db_session, teacher_id=teacher.id, student_id=student_b.id, session_type="group", slot_id=slot_id)
    session_a = _make_session(db_session, teacher_id=teacher.id, student_id=student_a.id, scheduled_at=now, booking_id=booking_a.id)
    session_b = _make_session(db_session, teacher_id=teacher.id, student_id=student_b.id, scheduled_at=now, booking_id=booking_b.id)

    await record_online_connect(session_a.id, _current_user(teacher, role="teacher"), db_session)

    db_session.refresh(session_a)
    db_session.refresh(session_b)
    assert session_a.teacher_joined_at is not None
    assert session_b.teacher_joined_at is not None  # fanned out to the sibling
    assert session_a.student_joined_at is None
    assert session_b.student_joined_at is None


# ─── No-show detector: candidate-query regression coverage ─────────────────

def test_no_show_candidate_query_includes_session_with_no_join_timestamp(db_session):
    """Exercises the EXACT candidate-selection query app/workers/
    booking_tasks.py's task_detect_online_teacher_no_show uses — proves a
    session that only ever had GET /room called on it (join timestamps
    still null) is correctly still a no-show candidate."""
    from sqlmodel import select

    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    old_enough = datetime.now(timezone.utc) - timedelta(hours=1)
    session = _make_session(db_session, teacher_id=teacher.id, student_id=student.id, scheduled_at=old_enough, status="live")

    candidates = db_session.exec(
        select(TutoringSession).where(
            TutoringSession.mode == "online",
            TutoringSession.status.in_(["scheduled", "waiting", "live"]),
            TutoringSession.teacher_joined_at.is_(None),
        )
    ).all()
    assert session.id in {c.id for c in candidates}


@pytest.mark.asyncio
async def test_no_show_candidate_query_excludes_session_after_real_connect(db_session):
    """The other half of the same regression: once record_online_connect
    has actually fired, the session must drop OUT of the no-show
    candidate set."""
    from sqlmodel import select
    from app.routers.session_validation import record_online_connect

    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    old_enough = datetime.now(timezone.utc) - timedelta(hours=1)
    session = _make_session(db_session, teacher_id=teacher.id, student_id=student.id, scheduled_at=old_enough, status="live")

    await record_online_connect(session.id, _current_user(teacher, role="teacher"), db_session)

    candidates = db_session.exec(
        select(TutoringSession).where(
            TutoringSession.mode == "online",
            TutoringSession.status.in_(["scheduled", "waiting", "live"]),
            TutoringSession.teacher_joined_at.is_(None),
        )
    ).all()
    assert session.id not in {c.id for c in candidates}


# ─── Post-call: teacher_end_session honors the scheduled_end gate ──────────

@pytest.mark.asyncio
async def test_teacher_end_session_before_scheduled_end_returns_409(db_session):
    from app.routers.session_validation import teacher_end_session

    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    session = _make_session(db_session, teacher_id=teacher.id, student_id=student.id, scheduled_at=datetime.now(timezone.utc), duration_min=60)

    fake_request = type("FakeRequest", (), {"client": None})()
    with pytest.raises(HTTPException) as exc_info:
        await teacher_end_session(session.id, fake_request, _current_user(teacher, role="teacher"), db_session)
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_teacher_end_session_after_scheduled_end_succeeds(db_session):
    from app.routers.session_validation import teacher_end_session

    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    started_long_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    session = _make_session(db_session, teacher_id=teacher.id, student_id=student.id, scheduled_at=started_long_ago, duration_min=60)

    fake_request = type("FakeRequest", (), {"client": None})()
    result = await teacher_end_session(session.id, fake_request, _current_user(teacher, role="teacher"), db_session)
    assert result["status"] == "awaiting_student_validation"


@pytest.mark.asyncio
async def test_teacher_end_session_denied_for_non_teacher(db_session):
    from app.routers.session_validation import teacher_end_session

    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    started_long_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    session = _make_session(db_session, teacher_id=teacher.id, student_id=student.id, scheduled_at=started_long_ago, duration_min=60)

    fake_request = type("FakeRequest", (), {"client": None})()
    with pytest.raises(HTTPException) as exc_info:
        await teacher_end_session(session.id, fake_request, _current_user(student), db_session)
    assert exc_info.value.status_code == 403


# ─── Group room-key resolution (pure functions, no network) ───────────────

def test_room_key_for_individual_session_uses_session_id(db_session):
    from app.services import livekit_video as lk_video

    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    session = _make_session(db_session, teacher_id=teacher.id, student_id=student.id, scheduled_at=datetime.now(timezone.utc))

    assert lk_video.room_key_for_session(db_session, session) == f"session-{session.id}"
    assert lk_video.group_slot_id(db_session, session) is None


def test_room_key_for_group_session_uses_shared_slot(db_session):
    from app.services import livekit_video as lk_video

    teacher = _make_profile(db_session)
    student_a = _make_profile(db_session)
    student_b = _make_profile(db_session)
    slot_id = uuid4()
    now = datetime.now(timezone.utc)

    booking_a = _make_booking(db_session, teacher_id=teacher.id, student_id=student_a.id, session_type="group", slot_id=slot_id)
    booking_b = _make_booking(db_session, teacher_id=teacher.id, student_id=student_b.id, session_type="group", slot_id=slot_id)
    session_a = _make_session(db_session, teacher_id=teacher.id, student_id=student_a.id, scheduled_at=now, booking_id=booking_a.id)
    session_b = _make_session(db_session, teacher_id=teacher.id, student_id=student_b.id, scheduled_at=now, booking_id=booking_b.id)

    key_a = lk_video.room_key_for_session(db_session, session_a)
    key_b = lk_video.room_key_for_session(db_session, session_b)
    assert key_a == key_b == f"slot-{slot_id}"

    siblings = lk_video.group_sessions(db_session, slot_id)
    assert {s.id for s in siblings} == {session_a.id, session_b.id}


def test_is_session_participant(db_session):
    from app.services import livekit_video as lk_video

    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    stranger = _make_profile(db_session)
    session = _make_session(db_session, teacher_id=teacher.id, student_id=student.id, scheduled_at=datetime.now(timezone.utc))

    assert lk_video.is_session_participant(db_session, session, teacher.id) is True
    assert lk_video.is_session_participant(db_session, session, student.id) is True
    assert lk_video.is_session_participant(db_session, session, stranger.id) is False


# ─── Dev-only test-tool endpoint must never work in production ────────────

@pytest.mark.asyncio
async def test_dev_simulate_start_blocked_in_production(db_session):
    from app.routers.classroom import dev_simulate_session_start

    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    session = _make_session(db_session, teacher_id=teacher.id, student_id=student.id, scheduled_at=datetime.now(timezone.utc) + timedelta(days=1))

    fake_settings = type("FakeSettings", (), {"is_production": True})()
    with patch("app.config.get_settings", return_value=fake_settings):
        with pytest.raises(HTTPException) as exc_info:
            await dev_simulate_session_start(session.id, _current_user(teacher, role="teacher"), db_session)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_dev_simulate_start_allowed_outside_production(db_session):
    from app.routers.classroom import dev_simulate_session_start

    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    future = datetime.now(timezone.utc) + timedelta(days=1)
    session = _make_session(db_session, teacher_id=teacher.id, student_id=student.id, scheduled_at=future)

    fake_settings = type("FakeSettings", (), {"is_production": False})()
    with patch("app.config.get_settings", return_value=fake_settings):
        result = await dev_simulate_session_start(session.id, _current_user(teacher, role="teacher"), db_session)
    assert result["status"] == "ok"
    db_session.refresh(session)
    # SQLite strips tzinfo on reload (see conftest.py's db_session fixture
    # docstring) — compare naively, same posture as test_phase16_hardening.py.
    scheduled_at_naive = session.scheduled_at.replace(tzinfo=None) if session.scheduled_at.tzinfo else session.scheduled_at
    future_naive = future.replace(tzinfo=None)
    assert scheduled_at_naive < future_naive
