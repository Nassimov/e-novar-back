from __future__ import annotations

"""Hour-based slot-splitting (docs/migrations — see app/routers/
student_teachers.py's _resolve_leg_range / _claim_slot_or_409). A teacher's
declared multi-hour availability (e.g. 12h-15h) must be independently
bookable in 1-hour consecutive units by different students, with the
server — never the frontend — recomputing duration/price and rejecting any
non-consecutive, out-of-window, or overlapping request.
"""

from datetime import date, time
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.booking import Booking
from app.models.profile import Profile
from app.models.scheduling import TeacherSlot


def _make_profile(db_session, **overrides):
    fields = {"id": uuid4(), "email": f"{uuid4()}@test.local", "first_name": "Test", "last_name": "User"}
    fields.update(overrides)
    profile = Profile(**fields)
    db_session.add(profile)
    db_session.commit()
    return profile


def _make_slot(db_session, *, teacher_id, start="12:00", end="15:00", slot_type="individual", max_students=1):
    slot = TeacherSlot(
        id=uuid4(), teacher_id=teacher_id, slot_date=date(2026, 9, 10),
        start_time=time.fromisoformat(start), end_time=time.fromisoformat(end),
        mode="online", type=slot_type, max_students=max_students, price=2000, status="open",
    )
    db_session.add(slot)
    db_session.commit()
    return slot


def _make_booking_on_slot(db_session, *, slot, student_id, teacher_id, start, duration_min, status="confirmed"):
    booking = Booking(
        id=uuid4(), student_id=student_id, teacher_id=teacher_id, slot_id=slot.id,
        booking_date=slot.slot_date, slot_time=time.fromisoformat(start), duration_min=duration_min,
        amount=0, status=status,
    )
    db_session.add(booking)
    db_session.commit()
    return booking


# ─── _resolve_leg_range — pure validation logic ─────────────────────────────

def test_resolve_leg_range_defaults_to_one_hour():
    from app.routers.student_teachers import _resolve_leg_range

    end_time, duration = _resolve_leg_range(
        slot=None, start_time=time(12, 0), requested_end_time=None, is_group=False,
    )
    assert end_time == time(13, 0)
    assert duration == 60


@pytest.mark.parametrize("hours", [1, 2, 3])
def test_resolve_leg_range_accepts_consecutive_whole_hours(hours):
    from app.routers.student_teachers import _resolve_leg_range

    slot = TeacherSlot(
        teacher_id=uuid4(), slot_date=date(2026, 9, 10),
        start_time=time(12, 0), end_time=time(15, 0), mode="online", type="individual", price=2000,
    )
    end_time, duration = _resolve_leg_range(
        slot=slot, start_time=time(12, 0), requested_end_time=time(12 + hours, 0), is_group=False,
    )
    assert duration == hours * 60


def test_resolve_leg_range_rejects_non_whole_hour_duration():
    from app.routers.student_teachers import _resolve_leg_range

    slot = TeacherSlot(
        teacher_id=uuid4(), slot_date=date(2026, 9, 10),
        start_time=time(12, 0), end_time=time(15, 0), mode="online", type="individual", price=2000,
    )
    with pytest.raises(HTTPException) as exc:
        _resolve_leg_range(slot=slot, start_time=time(12, 0), requested_end_time=time(12, 47), is_group=False)
    assert exc.value.status_code == 422


def test_resolve_leg_range_rejects_range_outside_slot_window():
    """A manually-crafted request naming an end_time past what the teacher
    actually declared must never be accepted — this is the server-side
    enforcement for "les créneaux doivent appartenir à la disponibilité du
    Teacher"."""
    from app.routers.student_teachers import _resolve_leg_range

    slot = TeacherSlot(
        teacher_id=uuid4(), slot_date=date(2026, 9, 10),
        start_time=time(12, 0), end_time=time(15, 0), mode="online", type="individual", price=2000,
    )
    with pytest.raises(HTTPException) as exc:
        _resolve_leg_range(slot=slot, start_time=time(14, 0), requested_end_time=time(16, 0), is_group=False)
    assert exc.value.status_code == 422


def test_resolve_leg_range_group_ignores_requested_end_time():
    """Group bookings are never time-split — always the slot's own full
    declared window, regardless of what a client sends as end_time."""
    from app.routers.student_teachers import _resolve_leg_range

    slot = TeacherSlot(
        teacher_id=uuid4(), slot_date=date(2026, 9, 10),
        start_time=time(12, 0), end_time=time(15, 0), mode="online", type="group", max_students=5, price=2000,
    )
    end_time, duration = _resolve_leg_range(
        slot=slot, start_time=time(12, 0), requested_end_time=time(13, 0), is_group=True,
    )
    assert end_time == time(15, 0)
    assert duration == 180


# ─── compute_variable_duration_amount — price scales with duration ─────────

def test_variable_duration_amount_single_hour():
    from app.services.pricing import compute_variable_duration_amount
    from app.models.admin import PlatformSettings

    settings = PlatformSettings(id=True)
    assert compute_variable_duration_amount([2000], "single", settings) == 2000


def test_variable_duration_amount_scales_with_hours():
    """1h=2000, 2h=4000, 3h=6000 — the exact example from the spec."""
    from app.services.pricing import compute_variable_duration_amount
    from app.models.admin import PlatformSettings

    settings = PlatformSettings(id=True)
    price_per_hour = 2000
    for hours, expected in [(1, 2000), (2, 4000), (3, 6000)]:
        leg_amount = round(price_per_hour * (hours * 60) / 60)
        assert compute_variable_duration_amount([leg_amount], "single", settings) == expected


def test_variable_duration_amount_pack5_applies_discount_to_real_total():
    from app.services.pricing import compute_variable_duration_amount
    from app.models.admin import PlatformSettings

    settings = PlatformSettings(id=True, pack5_discount_percent=10)
    # 3 one-hour sessions (2000 each) + 2 two-hour sessions (4000 each) = 14000
    leg_amounts = [2000, 2000, 2000, 4000, 4000]
    assert compute_variable_duration_amount(leg_amounts, "pack5", settings) == round(14000 * 0.9)


# ─── _claim_slot_or_409 — the actual race/overlap gate ──────────────────────

def test_claim_individual_slot_allows_independent_consecutive_hours(db_session):
    """The core scenario from the spec: a 12h-15h slot where one student
    books 12-13 must not block a DIFFERENT student from booking 13-14 or
    14-15 — only an actual time overlap should ever 409."""
    from app.routers.student_teachers import _claim_slot_or_409

    teacher = _make_profile(db_session)
    student_a = _make_profile(db_session)
    student_b = _make_profile(db_session)
    slot = _make_slot(db_session, teacher_id=teacher.id)

    _claim_slot_or_409(db_session, slot.id, student_a.id, is_group=False, start_time=time(12, 0), end_time=time(13, 0))
    _make_booking_on_slot(db_session, slot=slot, student_id=student_a.id, teacher_id=teacher.id, start="12:00", duration_min=60)

    # Different student, non-overlapping hour of the SAME slot — must succeed.
    _claim_slot_or_409(db_session, slot.id, student_b.id, is_group=False, start_time=time(13, 0), end_time=time(14, 0))

    db_session.refresh(slot)
    assert slot.status == "open"  # never flipped for a partially-booked individual slot


def test_claim_individual_slot_rejects_real_overlap(db_session):
    from app.routers.student_teachers import _claim_slot_or_409

    teacher = _make_profile(db_session)
    student_a = _make_profile(db_session)
    student_b = _make_profile(db_session)
    slot = _make_slot(db_session, teacher_id=teacher.id)

    _make_booking_on_slot(db_session, slot=slot, student_id=student_a.id, teacher_id=teacher.id, start="12:00", duration_min=120)  # 12-14

    # student_b wants 13-15 — overlaps student_a's 12-14 on the hour 13-14.
    with pytest.raises(HTTPException) as exc:
        _claim_slot_or_409(db_session, slot.id, student_b.id, is_group=False, start_time=time(13, 0), end_time=time(15, 0))
    assert exc.value.status_code == 409


def test_claim_group_slot_unchanged_capacity_behavior(db_session):
    """Group slots must keep the exact pre-existing behavior: capacity-
    gated by max_students, status flips to "booked" once full — no time
    splitting involved."""
    from app.routers.student_teachers import _claim_slot_or_409

    teacher = _make_profile(db_session)
    student_a = _make_profile(db_session)
    student_b = _make_profile(db_session)
    student_c = _make_profile(db_session)
    slot = _make_slot(db_session, teacher_id=teacher.id, slot_type="group", max_students=2)

    _claim_slot_or_409(db_session, slot.id, student_a.id, is_group=True, start_time=time(12, 0), end_time=time(15, 0))
    _make_booking_on_slot(db_session, slot=slot, student_id=student_a.id, teacher_id=teacher.id, start="12:00", duration_min=180)
    db_session.refresh(slot)
    assert slot.status == "open"  # 1 seat still free out of 2 — unchanged

    _claim_slot_or_409(db_session, slot.id, student_b.id, is_group=True, start_time=time(12, 0), end_time=time(15, 0))
    _make_booking_on_slot(db_session, slot=slot, student_id=student_b.id, teacher_id=teacher.id, start="12:00", duration_min=180)
    db_session.refresh(slot)
    assert slot.status == "booked"  # last seat just taken — matches pre-existing logic

    with pytest.raises(HTTPException) as exc:
        _claim_slot_or_409(db_session, slot.id, student_c.id, is_group=True, start_time=time(12, 0), end_time=time(15, 0))
    assert exc.value.status_code == 409
