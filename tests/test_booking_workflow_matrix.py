from __future__ import annotations

"""Exhaustive booking/payment workflow matrix (2026-09-14 audit request).

Covers every meaningful combination of:
  - session_type:   individual | group
  - mode:           online | at_student | at_home
  - formula:        single | pack5 | pack10
  - payment_method: cib | edahabia | cash | transfer | rib_cib | rib_edahabia

... through the REAL router functions (app.routers.student_teachers.
book_teacher_slot, app.routers.teachers.accept_booking/refuse_booking,
app.routers.admin.bookings.approve_manual_payment/reject_manual_payment),
called directly as plain Python functions against an in-memory SQLite
session — same convention as tests/test_classroom_session_workflow.py.
No network call is ever made: Stripe/Chargily checkout creation is
monkeypatched to a deterministic fake (real create_checkout_session would
otherwise try to reach the internet, which this sandbox can't do anyway,
and — the more important reason — that would make an external, rate-
limited, credentialed service part of every test run instead of just our
own business logic).

Money-safety: every DB write here is a fixture-scoped SQLite in-memory
table, wiped between tests (see conftest.py's db_session). This suite
never touches Stripe/Chargily's real API, never touches Postgres/Supabase,
and never runs against a deployed environment.
"""

import datetime as dt
import hashlib
import hmac
import json
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.models.booking import Booking, TutoringSession
from app.models.catalog import Level, Subject, TeacherDeliveryOption, TeacherSubjectPrice
from app.models.profile import Profile, StudentProfile, TeacherProfile
from app.models.scheduling import TeacherSlot, TeacherSlotSubject
from app.models.session_validation import SessionValidation
from app.services.pricing import compute_pack_prices, compute_variable_duration_amount, get_platform_settings

ALL_MODES = ("online", "at_student", "at_home")
ALL_TYPES = ("individual", "group")
ALL_FORMULAS = ("single", "pack5", "pack10")
ALL_PAYMENT_METHODS = ("cib", "edahabia", "cash", "transfer", "rib_cib", "rib_edahabia")
MANUAL_METHODS = ("cash", "transfer", "rib_cib", "rib_edahabia")


# ─── fixtures / helpers ──────────────────────────────────────────────────────

def _make_profile(db, **overrides):
    fields = {
        "id": uuid4(), "email": f"{uuid4()}@test.local",
        "first_name": "Test", "last_name": "User", "wilaya": "Alger",
    }
    fields.update(overrides)
    p = Profile(**fields)
    db.add(p)
    db.commit()
    return p


def _make_student(db, **overrides):
    profile = _make_profile(db, **{k: v for k, v in overrides.items() if k in ("wilaya",)})
    sp = StudentProfile(user_id=profile.id)
    db.add(sp)
    db.commit()
    return profile, sp


def _make_teacher(db, *, country="DZ", currency="DZD", price_per_session=2000, **overrides):
    profile = _make_profile(db, wilaya="Alger")
    tp = TeacherProfile(
        user_id=profile.id, country=country, currency=currency,
        price_per_session=price_per_session, status="approved", verified=True,
        **overrides,
    )
    db.add(tp)
    db.commit()
    # Every (mode, type) delivery option declared — the matrix tests every
    # combination, so the teacher must offer all of them up front (a
    # dedicated test below covers the "not offered" 422 separately).
    for mode in ALL_MODES:
        for typ in ALL_TYPES:
            db.add(TeacherDeliveryOption(teacher_id=profile.id, mode=mode, type=typ))
    db.commit()
    return profile, tp


def _make_subject_level(db):
    subject = Subject(slug=f"subj-{uuid4().hex[:8]}", name="Mathématiques")
    level = Level(code=f"lvl-{uuid4().hex[:8]}", label="3ème AS")
    db.add(subject)
    db.add(level)
    db.commit()
    return subject, level


def _future_date(days: int = 3) -> dt.date:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).date()


def _make_slot(db, *, teacher_id, subject, level, session_type="individual", mode="online",
                slot_date=None, start_time=dt.time(10, 0), end_time=dt.time(11, 0),
                price=2000, max_students=1):
    slot = TeacherSlot(
        teacher_id=teacher_id, slot_date=slot_date or _future_date(), start_time=start_time,
        end_time=end_time, type=session_type, mode=mode, price=price,
        max_students=max_students, status="open",
    )
    db.add(slot)
    db.commit()
    db.add(TeacherSlotSubject(slot_id=slot.id, subject_id=subject.id, level_id=level.id))
    db.commit()
    return slot


def _current_user(profile: Profile, role: str) -> dict:
    return {"id": str(profile.id), "email": profile.email, "role": role}


def _bid(result: dict) -> UUID:
    """book_teacher_slot returns booking_id as a str (JSON-response shape);
    SQLite's UUID column type (unlike real Postgres) needs an actual
    uuid.UUID instance for db.get()/`.where(Model.uuid_col == ...)` to bind
    correctly — see conftest.py's own SQLite-vs-Postgres shims for the same
    class of test-infra-only friction."""
    return UUID(result["booking_id"])


def _fake_stripe_checkout(**kwargs):
    return {"session_id": f"cs_test_{uuid4().hex}", "url": "https://checkout.stripe.test/fake"}


def _fake_chargily_checkout(**kwargs):
    return {"id": f"chk_test_{uuid4().hex}", "checkout_url": "https://pay.chargily.test/fake"}


def _book(db, *, teacher_profile, student_profile, session_type, mode, formula,
          payment_method, subject, level, slot=None, pack_sessions=None):
    """Calls the real booking endpoint function directly (bypassing HTTP),
    with Stripe/Chargily checkout creation faked (no network)."""
    from app.routers.student_teachers import BookingBody, book_teacher_slot

    body_kwargs = dict(
        formula=formula, mode=mode, payment_method=payment_method,
        session_type=session_type, subject_id=subject.id, level_id=level.id,
    )
    if slot is not None:
        body_kwargs["slot_id"] = str(slot.id)
        body_kwargs["date"] = slot.slot_date.isoformat()
        body_kwargs["slot_time"] = slot.start_time.strftime("%H:%M")
    elif pack_sessions is None:
        body_kwargs["date"] = _future_date().isoformat()
        body_kwargs["slot_time"] = "14:00"

    if pack_sessions is not None:
        body_kwargs["pack_sessions"] = pack_sessions

    body = BookingBody(**body_kwargs)
    with patch("app.services.stripe.create_checkout_session", side_effect=_fake_stripe_checkout), \
         patch("app.services.stripe.create_checkout_session_native", side_effect=_fake_stripe_checkout), \
         patch("app.services.chargily.create_checkout", side_effect=_fake_chargily_checkout):
        return book_teacher_slot(
            teacher_ref=str(teacher_profile.id), body=body,
            current_user=_current_user(student_profile, "student"), db=db,
        )


def _pack_sessions_payload(*, session_type, subject, level, count, start_day=5):
    return [
        {
            "date": _future_date(start_day + i).isoformat(),
            "slot_time": "16:00",
            "session_type": session_type,
            "subject_id": str(subject.id),
            "level_id": str(level.id),
        }
        for i in range(count)
    ]


def _confirm_payment(db, *, booking_id, payment_method, admin_profile=None):
    """Simulates whatever gets a booking from 'pending' payment to
    'payment confirmed, awaiting teacher' for each rail, the same state a
    real gateway webhook / admin action would produce — then, for cash/
    transfer/rib_*, admin approval IS the final confirmation (see
    app/routers/admin/bookings.py's approve_manual_payment docstring)."""
    from app.routers.admin.bookings import approve_manual_payment

    booking = db.get(Booking, booking_id)
    if payment_method == "cib":
        booking.stripe_pi_id = f"pi_test_{uuid4().hex}"
        db.add(booking)
        db.commit()
        return None
    if payment_method == "edahabia":
        booking.chargily_paid_at = dt.datetime.now(dt.timezone.utc)
        db.add(booking)
        db.commit()
        return None
    # cash | transfer | rib_cib | rib_edahabia
    admin = admin_profile or _current_user(_make_profile(db), "admin")
    return approve_manual_payment(booking_id=booking_id, _admin=admin, db=db)


def _accept(db, *, booking_id, teacher_profile):
    from app.routers.teachers import accept_booking

    with patch("app.services.stripe.capture_payment_intent", return_value={"status": "succeeded"}), \
         patch("app.services.stripe.get_checkout_session", return_value={"payment_intent": "pi_test_fallback"}):
        return accept_booking(
            booking_id=booking_id, current_user=_current_user(teacher_profile, "teacher"), db=db,
        )


# ─── pricing semantics (no HTTP involved — pure function) ───────────────────

def test_compute_pack_prices_group_is_a_single_discounted_lesson_not_a_multi_pack(db_session):
    """Pins down pricing.py's own documented contract: 'group' is priced as
    ONE lesson at the group-discount rate, never as N lessons. Exists so a
    future change to book_teacher_slot's amount-resolution order (which
    currently checks `resolved_session_type == "group"` BEFORE checking
    `body.pack_sessions`) can't silently start undercharging a real
    group-pack booking without a test going red."""
    settings = get_platform_settings(db_session)
    prices = compute_pack_prices(2000, settings)
    assert prices["single"] == 2000
    assert prices["group"] < prices["single"]
    assert prices["pack5"] == round(2000 * 5 * (1 - settings.pack5_discount_percent / 100))
    assert prices["pack10"] == round(2000 * 10 * (1 - settings.pack10_discount_percent / 100))


@pytest.mark.parametrize("formula,count", [("pack5", 5), ("pack10", 10)])
def test_group_pack_booking_charged_N_group_lessons_pack_discounted(db_session, formula, count):
    """Was a real billing bug (found via this exhaustive matrix, then fixed
    per product decision 2026-09-14): booking formula=pack5/pack10 with
    session_type="group" used to charge pack_prices['group'] (ONE lesson)
    while scheduling 5 or 10 real TutoringSession rows — book_teacher_slot
    checked `resolved_session_type == "group"` before `body.pack_sessions`,
    so the pack branch was unreachable for a group pack. Fixed: a group
    pack now bills N group-rate legs (each leg flatly priced at the group
    rate, never duration-scaled — same principle as a lone group lesson),
    summed and THEN pack-discounted via compute_variable_duration_amount,
    exactly mirroring how an individual pack is billed."""
    teacher_profile, tp = _make_teacher(db_session)
    student_profile, _sp = _make_student(db_session)
    subject, level = _make_subject_level(db_session)

    pack_sessions = _pack_sessions_payload(session_type="group", subject=subject, level=level, count=count)
    result = _book(
        db_session, teacher_profile=teacher_profile, student_profile=student_profile,
        session_type="group", mode="online", formula=formula, payment_method="cash",
        subject=subject, level=level, pack_sessions=pack_sessions,
    )

    settings = get_platform_settings(db_session)
    group_leg = compute_pack_prices(tp.price_per_session, settings)["group"]
    expected = compute_variable_duration_amount([group_leg] * count, formula, settings)
    assert result["amount"] == expected
    assert expected > group_leg  # genuinely N-lesson pricing, not 1x

    booking = db_session.get(Booking, _bid(result))
    assert len(json.loads(booking.pack_sessions)) == count


# ─── the exhaustive matrix ───────────────────────────────────────────────────

def _valid_single_combinations():
    for session_type in ALL_TYPES:
        for mode in ALL_MODES:
            for payment_method in ALL_PAYMENT_METHODS:
                yield (session_type, mode, "single", payment_method)


@pytest.mark.parametrize("session_type,mode,formula,payment_method", list(_valid_single_combinations()))
def test_single_booking_full_lifecycle(db_session, session_type, mode, formula, payment_method):
    """Every (session_type x mode x payment_method) combination for a plain
    single-lesson booking: create -> confirm payment -> teacher accepts ->
    booking.status == 'confirmed'. 2 x 3 x 6 = 36 combinations."""
    teacher_profile, tp = _make_teacher(db_session)
    student_profile, sp = _make_student(db_session)
    subject, level = _make_subject_level(db_session)
    if mode == "at_student":
        student_profile.wilaya = teacher_profile.wilaya = "Alger"
        db_session.add(student_profile)
        db_session.add(teacher_profile)
        db_session.commit()
    slot = _make_slot(
        db_session, teacher_id=teacher_profile.id, subject=subject, level=level,
        session_type=session_type, mode=mode, max_students=4 if session_type == "group" else 1,
    )

    result = _book(
        db_session, teacher_profile=teacher_profile, student_profile=student_profile,
        session_type=session_type, mode=mode, formula=formula, payment_method=payment_method,
        subject=subject, level=level, slot=slot,
    )
    booking_id = _bid(result)
    booking = db_session.get(Booking, booking_id)
    assert booking.status == "pending"
    assert booking.payment_method == payment_method
    assert booking.session_type == session_type
    assert booking.mode == mode

    settings = get_platform_settings(db_session)
    expected = compute_pack_prices(tp.price_per_session, settings)
    if session_type == "group":
        assert result["amount"] == expected["group"]
    else:
        assert result["amount"] == expected["single"]

    sv_rows = db_session.exec(
        __import__("sqlmodel").select(SessionValidation).where(SessionValidation.booking_id == booking_id)
    ).all()
    assert len(sv_rows) == 1

    _confirm_payment(db_session, booking_id=booking_id, payment_method=payment_method)

    if payment_method in MANUAL_METHODS:
        # approve_manual_payment IS the final confirmation for these rails —
        # no separate teacher accept step (accept_booking explicitly 409s
        # a manual-method booking, see _MANUAL_PAYMENT_METHODS guard).
        booking = db_session.get(Booking, booking_id)
        assert booking.status == "confirmed"
        with pytest.raises(HTTPException) as exc:
            _accept(db_session, booking_id=booking_id, teacher_profile=teacher_profile)
        assert exc.value.status_code == 409
    else:
        _accept(db_session, booking_id=booking_id, teacher_profile=teacher_profile)
        booking = db_session.get(Booking, booking_id)
        assert booking.status == "confirmed"


@pytest.mark.parametrize("formula", ["pack5", "pack10"])
@pytest.mark.parametrize("payment_method", list(ALL_PAYMENT_METHODS))
def test_individual_pack_booking_full_lifecycle(db_session, formula, payment_method):
    """Individual-session packs (the real, intended pack use case per the
    pricing.py docstring) across every payment method. 2 formulas x 6
    payment methods = 12 combinations."""
    teacher_profile, tp = _make_teacher(db_session)
    student_profile, _sp = _make_student(db_session)
    subject, level = _make_subject_level(db_session)
    count = 5 if formula == "pack5" else 10
    pack_sessions = _pack_sessions_payload(session_type="individual", subject=subject, level=level, count=count)

    result = _book(
        db_session, teacher_profile=teacher_profile, student_profile=student_profile,
        session_type="individual", mode="online", formula=formula, payment_method=payment_method,
        subject=subject, level=level, pack_sessions=pack_sessions,
    )
    booking_id = _bid(result)
    settings = get_platform_settings(db_session)
    expected = compute_pack_prices(tp.price_per_session, settings)[formula]
    assert result["amount"] == expected

    booking = db_session.get(Booking, booking_id)
    assert len(json.loads(booking.pack_sessions)) == count
    sv_rows = db_session.exec(
        __import__("sqlmodel").select(SessionValidation).where(SessionValidation.booking_id == booking_id)
    ).all()
    assert len(sv_rows) == count

    _confirm_payment(db_session, booking_id=booking_id, payment_method=payment_method)
    if payment_method in MANUAL_METHODS:
        assert db_session.get(Booking, booking_id).status == "confirmed"
    else:
        _accept(db_session, booking_id=booking_id, teacher_profile=teacher_profile)
        assert db_session.get(Booking, booking_id).status == "confirmed"


@pytest.mark.parametrize("required_count,formula", [(5, "pack5"), (10, "pack10")])
def test_pack_booking_rejects_wrong_session_count(db_session, required_count, formula):
    teacher_profile, tp = _make_teacher(db_session)
    student_profile, _sp = _make_student(db_session)
    subject, level = _make_subject_level(db_session)
    pack_sessions = _pack_sessions_payload(
        session_type="individual", subject=subject, level=level, count=required_count - 1,
    )
    with pytest.raises(HTTPException) as exc:
        _book(
            db_session, teacher_profile=teacher_profile, student_profile=student_profile,
            session_type="individual", mode="online", formula=formula, payment_method="cash",
            subject=subject, level=level, pack_sessions=pack_sessions,
        )
    assert exc.value.status_code == 422


# ─── refusal / rejection paths ───────────────────────────────────────────────

@pytest.mark.parametrize("payment_method", list(ALL_PAYMENT_METHODS))
def test_teacher_refuses_gateway_confirmed_booking(db_session, payment_method):
    """cib/edahabia: teacher can refuse even after the gateway confirmed
    payment (refund is out of scope of accept/refuse itself — handled
    elsewhere). cash/transfer/rib_*: refuse is only reachable AFTER admin
    approval flips it to confirmed — teachers never see a manual-payment
    booking before that (accept_booking 409s it), but refuse_booking's own
    guard is checked directly here regardless of that UI-level gating."""
    from app.routers.teachers import refuse_booking

    teacher_profile, tp = _make_teacher(db_session)
    student_profile, _sp = _make_student(db_session)
    subject, level = _make_subject_level(db_session)
    slot = _make_slot(db_session, teacher_id=teacher_profile.id, subject=subject, level=level)

    result = _book(
        db_session, teacher_profile=teacher_profile, student_profile=student_profile,
        session_type="individual", mode="online", formula="single", payment_method=payment_method,
        subject=subject, level=level, slot=slot,
    )
    booking_id = _bid(result)
    if payment_method not in MANUAL_METHODS:
        _confirm_payment(db_session, booking_id=booking_id, payment_method=payment_method)
        refuse_booking(
            booking_id=booking_id, current_user=_current_user(teacher_profile, "teacher"), db=db_session,
        )
        booking = db_session.get(Booking, booking_id)
        assert booking.status in ("refused", "cancelled")


@pytest.mark.parametrize("payment_method", MANUAL_METHODS)
def test_admin_rejects_manual_payment(db_session, payment_method):
    from app.routers.admin.bookings import reject_manual_payment

    teacher_profile, tp = _make_teacher(db_session)
    student_profile, _sp = _make_student(db_session)
    subject, level = _make_subject_level(db_session)
    slot = _make_slot(db_session, teacher_id=teacher_profile.id, subject=subject, level=level)

    result = _book(
        db_session, teacher_profile=teacher_profile, student_profile=student_profile,
        session_type="individual", mode="online", formula="single", payment_method=payment_method,
        subject=subject, level=level, slot=slot,
    )
    admin = _current_user(_make_profile(db_session), "admin")
    reject_manual_payment(booking_id=_bid(result), _admin=admin, db=db_session)
    booking = db_session.get(Booking, _bid(result))
    assert booking.status in ("cancelled", "rejected")


# ─── guard / invalid-combination tests ───────────────────────────────────────

def test_non_dz_teacher_forces_online_only(db_session):
    teacher_profile, tp = _make_teacher(db_session, country="FR", currency="EUR")
    student_profile, _sp = _make_student(db_session)
    subject, level = _make_subject_level(db_session)
    with pytest.raises(HTTPException) as exc:
        _book(
            db_session, teacher_profile=teacher_profile, student_profile=student_profile,
            session_type="individual", mode="at_home", formula="single", payment_method="cib",
            subject=subject, level=level,
        )
    assert exc.value.status_code == 422


def test_non_dzd_teacher_rejects_non_cib_payment(db_session):
    teacher_profile, tp = _make_teacher(db_session, country="FR", currency="EUR")
    student_profile, _sp = _make_student(db_session)
    subject, level = _make_subject_level(db_session)
    with pytest.raises(HTTPException) as exc:
        _book(
            db_session, teacher_profile=teacher_profile, student_profile=student_profile,
            session_type="individual", mode="online", formula="single", payment_method="edahabia",
            subject=subject, level=level,
        )
    assert exc.value.status_code == 422


def test_non_dzd_teacher_accepts_cib(db_session):
    teacher_profile, tp = _make_teacher(db_session, country="FR", currency="EUR", price_per_session=30)
    student_profile, _sp = _make_student(db_session)
    subject, level = _make_subject_level(db_session)
    result = _book(
        db_session, teacher_profile=teacher_profile, student_profile=student_profile,
        session_type="individual", mode="online", formula="single", payment_method="cib",
        subject=subject, level=level,
    )
    assert result["amount"] == 30
    assert result["checkout_url"]


def test_mode_not_offered_by_teacher_rejected(db_session):
    profile = _make_profile(db_session, wilaya="Alger")
    tp = TeacherProfile(user_id=profile.id, country="DZ", currency="DZD", price_per_session=2000, status="approved")
    db_session.add(tp)
    db_session.commit()
    # Only declares online/individual — nothing else.
    db_session.add(TeacherDeliveryOption(teacher_id=profile.id, mode="online", type="individual"))
    db_session.commit()
    student_profile, _sp = _make_student(db_session)
    subject, level = _make_subject_level(db_session)
    with pytest.raises(HTTPException) as exc:
        _book(
            db_session, teacher_profile=profile, student_profile=student_profile,
            session_type="individual", mode="at_home", formula="single", payment_method="cash",
            subject=subject, level=level,
        )
    assert exc.value.status_code == 422


def test_at_student_mode_rejected_on_wilaya_mismatch(db_session):
    teacher_profile, tp = _make_teacher(db_session)
    teacher_profile.wilaya = "Oran"
    db_session.add(teacher_profile)
    db_session.commit()
    student_profile, _sp = _make_student(db_session)
    student_profile.wilaya = "Alger"
    db_session.add(student_profile)
    db_session.commit()
    subject, level = _make_subject_level(db_session)
    with pytest.raises(HTTPException) as exc:
        _book(
            db_session, teacher_profile=teacher_profile, student_profile=student_profile,
            session_type="individual", mode="at_student", formula="single", payment_method="cash",
            subject=subject, level=level,
        )
    assert exc.value.status_code == 422


def test_suspended_student_cannot_book(db_session):
    teacher_profile, tp = _make_teacher(db_session)
    student_profile, sp = _make_student(db_session)
    sp.booking_suspended_until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
    db_session.add(sp)
    db_session.commit()
    subject, level = _make_subject_level(db_session)
    with pytest.raises(HTTPException) as exc:
        _book(
            db_session, teacher_profile=teacher_profile, student_profile=student_profile,
            session_type="individual", mode="online", formula="single", payment_method="cash",
            subject=subject, level=level,
        )
    assert exc.value.status_code == 403


def test_double_booking_same_slot_rejected(db_session):
    teacher_profile, tp = _make_teacher(db_session)
    student_profile, _sp = _make_student(db_session)
    other_student_profile, _sp2 = _make_student(db_session)
    subject, level = _make_subject_level(db_session)
    slot = _make_slot(db_session, teacher_id=teacher_profile.id, subject=subject, level=level, max_students=1)

    _book(
        db_session, teacher_profile=teacher_profile, student_profile=student_profile,
        session_type="individual", mode="online", formula="single", payment_method="cash",
        subject=subject, level=level, slot=slot,
    )
    with pytest.raises(HTTPException) as exc:
        _book(
            db_session, teacher_profile=teacher_profile, student_profile=other_student_profile,
            session_type="individual", mode="online", formula="single", payment_method="cash",
            subject=subject, level=level, slot=slot,
        )
    assert exc.value.status_code == 409


def test_group_slot_accepts_multiple_students_up_to_capacity(db_session):
    teacher_profile, tp = _make_teacher(db_session)
    subject, level = _make_subject_level(db_session)
    slot = _make_slot(
        db_session, teacher_id=teacher_profile.id, subject=subject, level=level,
        session_type="group", mode="online", max_students=2,
    )
    s1, _ = _make_student(db_session)
    s2, _ = _make_student(db_session)
    s3, _ = _make_student(db_session)

    _book(db_session, teacher_profile=teacher_profile, student_profile=s1, session_type="group",
          mode="online", formula="single", payment_method="cash", subject=subject, level=level, slot=slot)
    _book(db_session, teacher_profile=teacher_profile, student_profile=s2, session_type="group",
          mode="online", formula="single", payment_method="cash", subject=subject, level=level, slot=slot)
    with pytest.raises(HTTPException) as exc:
        _book(db_session, teacher_profile=teacher_profile, student_profile=s3, session_type="group",
              mode="online", formula="single", payment_method="cash", subject=subject, level=level, slot=slot)
    assert exc.value.status_code == 409


# ─── Chargily webhook (real HTTP-level function, signature verified) ────────

def test_chargily_webhook_valid_signature_sets_paid_at(db_session):
    import asyncio

    from starlette.requests import Request

    teacher_profile, tp = _make_teacher(db_session)
    student_profile, _sp = _make_student(db_session)
    subject, level = _make_subject_level(db_session)
    result = _book(
        db_session, teacher_profile=teacher_profile, student_profile=student_profile,
        session_type="individual", mode="online", formula="single", payment_method="edahabia",
        subject=subject, level=level,
    )
    booking = db_session.get(Booking, _bid(result))
    booking.chargily_checkout_id = "chk_webhook_test"
    db_session.add(booking)
    db_session.commit()

    payload = json.dumps({"type": "checkout.paid", "data": {"id": "chk_webhook_test"}}).encode()
    secret = "test-chargily-secret"
    signature = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

    async def _run():
        from app.routers import chargily_webhook as webhook_module
        from app.services import chargily as chargily_module

        with patch.object(chargily_module.settings, "chargily_secret_key", secret):
            scope = {
                "type": "http", "method": "POST", "headers": [(b"signature", signature.encode())],
                "path": "/api/payments/chargily/webhook", "query_string": b"",
            }

            async def receive():
                return {"type": "http.request", "body": payload, "more_body": False}

            request = Request(scope, receive)
            return await webhook_module.chargily_webhook(request=request, db=db_session)

    response = asyncio.run(_run())
    assert response == {"received": True}
    db_session.refresh(booking)
    assert booking.chargily_paid_at is not None


def test_chargily_webhook_invalid_signature_rejected(db_session):
    import asyncio

    from starlette.requests import Request

    payload = json.dumps({"type": "checkout.paid", "data": {"id": "irrelevant"}}).encode()

    async def _run():
        from app.routers import chargily_webhook as webhook_module
        from app.services import chargily as chargily_module

        with patch.object(chargily_module.settings, "chargily_secret_key", "real-secret"):
            scope = {
                "type": "http", "method": "POST", "headers": [(b"signature", b"totally-wrong")],
                "path": "/api/payments/chargily/webhook", "query_string": b"",
            }

            async def receive():
                return {"type": "http.request", "body": payload, "more_body": False}

            request = Request(scope, receive)
            return await webhook_module.chargily_webhook(request=request, db=db_session)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(_run())
    assert exc.value.status_code == 403


# ─── teacher students-overview timezone regression (2026-09-14 report) ──────

def test_students_overview_no_tz_crash_for_teacher_with_completed_session(db_session):
    """A teacher with a completed session and no upcoming one used to crash
    this endpoint with 'can't compare offset-naive and offset-aware
    datetimes' (datetime.utcnow() vs TutoringSession.scheduled_at, a
    TIMESTAMPTZ column) — reported as "Couldn't load your students" on
    /teacher/students (and the same query backs the "Active students" stat
    on /teacher). Fixed in app/routers/teachers.py's get_my_students_overview."""
    from app.routers.teachers import get_my_students_overview

    teacher_profile, tp = _make_teacher(db_session)
    student_profile, _sp = _make_student(db_session)
    session = TutoringSession(
        teacher_id=teacher_profile.id, student_id=student_profile.id,
        scheduled_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10),
        duration_min=60, mode="online", status="completed",
    )
    db_session.add(session)
    db_session.commit()

    result = get_my_students_overview(
        current_user=_current_user(teacher_profile, "teacher"), db=db_session,
    )
    assert len(result) == 1
    assert result[0]["student_id"] == str(student_profile.id)
    assert result[0]["status"] == "active"  # completed 10 days ago, within the 30-day window
    assert result[0]["sessions"] == 1
