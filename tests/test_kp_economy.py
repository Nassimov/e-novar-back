from __future__ import annotations

"""Admin-configurable EP economy + commission mechanism (business/EP audit
follow-up, 2026-09-08, migration 110). Every business value here is
admin-settable via PlatformSettings — nothing is decided by this code, only
the mechanism. Also covers the two previously-dead KP-granting DB triggers
now reimplemented for real in Python (lesson-completion EP, wired to be
reversible on admin rejection)."""

import datetime as dt
import json
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from app.models.admin import PlatformSettings
from app.models.booking import Booking, TutoringSession
from app.models.enums import KpSource
from app.models.profile import Profile, TeacherProfile
from app.models.session_validation import SessionValidation
from app.services.boost import activate_boost, get_boost_plans
from app.services.kp import award_kp, get_or_create_kp_account, reverse_kp_transaction
from app.services.referral import apply_referral_code
from app.services.session_validation import credit_session_payout


def _make_profile(db_session, **overrides):
    fields = {"id": uuid4(), "email": f"{uuid4()}@test.local", "first_name": "Test", "last_name": "User"}
    fields.update(overrides)
    profile = Profile(**fields)
    db_session.add(profile)
    db_session.commit()
    return profile


def _set_platform_settings(monkeypatch, **overrides) -> PlatformSettings:
    """Every module under test does a local `from app.services.pricing
    import get_platform_settings` inside the function body (re-resolved on
    every call, not bound at import time), so patching the module-level
    name here reaches all of them. Never touches the DB: constructing
    PlatformSettings(id=True, ...) as a plain Python object is fine — it's
    only actually INSERTing a full row that fails under SQLite (several
    ARRAY-typed columns, e.g. booking_no_response_suspension_days, have no
    SQLite bind support — a pre-existing test-DB limitation unrelated to
    this feature), which this sidesteps entirely."""
    settings = PlatformSettings(id=True, **overrides)
    monkeypatch.setattr("app.services.pricing.get_platform_settings", lambda db: settings)
    return settings


def _seed_full_platform_settings_row(db_session) -> None:
    """For the one test that needs a REAL, persisted, fully-populated
    settings row (testing the admin endpoint's own read/write, not code
    that merely reads settings) — a plain ORM insert of PlatformSettings
    hits the same SQLite ARRAY-binding limitation described above. Bypasses
    it by JSON-serializing just the ARRAY/JSONB-typed columns for a raw
    INSERT, using PlatformSettings' own Python defaults for every other
    (ordinary, bindable) column — a test-only workaround, not a general
    ARRAY/SQLite fix (a global one was tried and reverted in conftest.py;
    see its comment for why)."""
    defaults = PlatformSettings(id=True)
    table = PlatformSettings.__table__
    values: dict = {}
    for col in table.columns:
        if col.name == "id":
            values["id"] = 1
            continue
        val = getattr(defaults, col.name)
        if isinstance(col.type, (ARRAY, JSONB)) and val is not None:
            val = json.dumps(val)
        values[col.name] = val
    col_names = list(values.keys())
    db_session.execute(
        text(f"INSERT INTO platform_settings ({', '.join(col_names)}) VALUES ({', '.join(f':{n}' for n in col_names)})"),
        values,
    )
    db_session.commit()


def _make_session_and_validation(db_session, *, student, teacher, amount, formula="single", kp_reward=0):
    booking = Booking(
        id=uuid4(), student_id=student.id, teacher_id=teacher.id,
        booking_date=dt.date(2026, 9, 10), amount=amount, formula=formula,
        kp_reward=kp_reward, status="confirmed",
    )
    db_session.add(booking)
    db_session.commit()

    session = TutoringSession(
        id=uuid4(), booking_id=booking.id, teacher_id=teacher.id, student_id=student.id,
        scheduled_at=dt.datetime(2026, 9, 10, 10, 0, tzinfo=dt.timezone.utc), status="scheduled",
    )
    db_session.add(session)
    db_session.commit()

    sv = SessionValidation(
        id=uuid4(), session_id=session.id, booking_id=booking.id,
        student_id=student.id, teacher_id=teacher.id, status="validated",
    )
    db_session.add(sv)
    db_session.commit()

    return booking, session, sv


# ── commission mechanism ────────────────────────────────────────────────────

def test_credit_session_payout_defaults_to_zero_commission(db_session):
    """No admin action taken yet (commission_percent defaults to 0) —
    payout must equal today's 100% passthrough exactly."""
    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    db_session.add(TeacherProfile(user_id=teacher.id))
    db_session.commit()

    booking, session, sv = _make_session_and_validation(db_session, student=student, teacher=teacher, amount=1000)

    payout = credit_session_payout(db_session, session, sv)

    assert payout == 1000
    assert session.platform_commission_amount == 0
    tp = db_session.get(TeacherProfile, teacher.id)
    assert tp.wallet_balance_dzd == 1000


def test_credit_session_payout_applies_admin_configured_commission(db_session, monkeypatch):
    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    db_session.add(TeacherProfile(user_id=teacher.id))
    db_session.commit()
    _set_platform_settings(monkeypatch, commission_percent=20)

    booking, session, sv = _make_session_and_validation(db_session, student=student, teacher=teacher, amount=1000)

    payout = credit_session_payout(db_session, session, sv)

    assert payout == 800
    assert session.platform_commission_amount == 200
    tp = db_session.get(TeacherProfile, teacher.id)
    assert tp.wallet_balance_dzd == 800


def test_credit_session_payout_grants_student_lesson_kp(db_session):
    """The advertised booking.kp_reward was never actually granted before
    (a dead DB trigger, see migration 110) — now granted here for real."""
    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    db_session.add(TeacherProfile(user_id=teacher.id))
    db_session.commit()

    booking, session, sv = _make_session_and_validation(
        db_session, student=student, teacher=teacher, amount=1000, kp_reward=30,
    )
    credit_session_payout(db_session, session, sv)

    account = get_or_create_kp_account(student.id, db_session)
    assert account.balance == 30


def test_lesson_kp_is_reversible_via_ref_type_booking_completed(db_session):
    """Mirrors what app/routers/admin/session_validation.py's
    reject_validation now does when a session already credited gets
    rejected after the fact — the student shouldn't keep EP for a lesson
    an admin just ruled didn't happen."""
    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    db_session.add(TeacherProfile(user_id=teacher.id))
    db_session.commit()

    booking, session, sv = _make_session_and_validation(
        db_session, student=student, teacher=teacher, amount=1000, kp_reward=30,
    )
    credit_session_payout(db_session, session, sv)
    assert get_or_create_kp_account(student.id, db_session).balance == 30

    reverse_kp_transaction(
        ref_type="booking_completed", ref_id=session.id,
        reason="Séance rejetée après vérification administrative", db=db_session,
    )
    assert get_or_create_kp_account(student.id, db_session).balance == 0


# ── daily caps (Point 5.4 — farming limits) ─────────────────────────────────

def test_award_kp_uncapped_source_is_unaffected(db_session):
    student = _make_profile(db_session)
    award_kp(student.id, 1000, KpSource.quiz, "Quiz", db_session)
    assert get_or_create_kp_account(student.id, db_session).balance == 1000


def test_award_kp_clamps_to_admin_configured_daily_cap(db_session, monkeypatch):
    student = _make_profile(db_session)
    _set_platform_settings(monkeypatch, kp_source_daily_caps={"quiz": 50})

    award_kp(student.id, 40, KpSource.quiz, "Quiz 1", db_session)
    assert get_or_create_kp_account(student.id, db_session).balance == 40

    # Only 10 of the requested 40 fits under today's remaining cap.
    award_kp(student.id, 40, KpSource.quiz, "Quiz 2", db_session)
    assert get_or_create_kp_account(student.id, db_session).balance == 50

    # Cap fully used — a further award today is a graceful no-op, not an error.
    account, leveled_up = award_kp(student.id, 10, KpSource.quiz, "Quiz 3", db_session)
    assert account.balance == 50
    assert leveled_up is False


def test_award_kp_daily_cap_does_not_affect_other_sources(db_session, monkeypatch):
    student = _make_profile(db_session)
    _set_platform_settings(monkeypatch, kp_source_daily_caps={"quiz": 10})

    award_kp(student.id, 10, KpSource.quiz, "Quiz", db_session)
    award_kp(student.id, 500, KpSource.homework, "Devoir", db_session)

    assert get_or_create_kp_account(student.id, db_session).balance == 510


# ── admin-configurable referral / boost amounts ─────────────────────────────

def test_referral_welcome_bonus_uses_admin_configured_amount(db_session, monkeypatch):
    referrer = _make_profile(db_session)
    referrer.referral_code = "TESTCODE"
    db_session.add(referrer)
    db_session.commit()

    _set_platform_settings(monkeypatch, kp_referral_referee_student=555)

    referee = _make_profile(db_session)
    result = apply_referral_code(referee.id, "student", "TESTCODE", db_session)

    assert result["kp_earned"] == 555
    assert get_or_create_kp_account(referee.id, db_session).balance == 555


def test_boost_cost_uses_admin_configured_amount(db_session, monkeypatch):
    teacher = _make_profile(db_session)
    tp = TeacherProfile(user_id=teacher.id)
    db_session.add(tp)
    db_session.commit()
    award_kp(teacher.id, 1000, KpSource.reward, "Crédit test", db_session)

    _set_platform_settings(monkeypatch, kp_boost_cost_7d=42)
    assert get_boost_plans(db_session)[7] == 42

    activate_boost(tp, 7, db_session)

    assert get_or_create_kp_account(teacher.id, db_session).balance == 1000 - 42


def test_boost_replayed_idempotency_key_does_not_double_charge_or_double_extend(db_session):
    """A double-click/retry of the SAME purchase attempt (same
    idempotency_key) must neither charge EP twice nor extend the boost
    twice — but a second, genuinely new attempt (different key) still
    legitimately stacks more time."""
    teacher = _make_profile(db_session)
    tp = TeacherProfile(user_id=teacher.id)
    db_session.add(tp)
    db_session.commit()
    award_kp(teacher.id, 1000, KpSource.reward, "Crédit test", db_session)

    key = "attempt-1"
    activate_boost(tp, 7, db_session, idempotency_key=key)
    balance_after_first = get_or_create_kp_account(teacher.id, db_session).balance
    expiry_after_first = tp.boost_expires_at

    activate_boost(tp, 7, db_session, idempotency_key=key)  # replay of the same attempt
    assert get_or_create_kp_account(teacher.id, db_session).balance == balance_after_first
    assert tp.boost_expires_at == expiry_after_first

    activate_boost(tp, 7, db_session, idempotency_key="attempt-2")  # a new, deliberate purchase
    assert get_or_create_kp_account(teacher.id, db_session).balance < balance_after_first
    assert tp.boost_expires_at > expiry_after_first


def test_club_creation_replayed_idempotency_key_is_rejected_not_double_charged(db_session, monkeypatch):
    from app.services.club import club_service

    _set_platform_settings(monkeypatch, kp_referral_referee_student=100)  # no-op, just to exercise the patch path
    owner = _make_profile(db_session)
    award_kp(owner.id, 1000, KpSource.reward, "Crédit test", db_session)

    def _fake_settings(db):
        from app.models.admin import PlatformSettings
        return PlatformSettings(id=True, competitive_club_creation_cost_ep=100)

    monkeypatch.setattr("app.services.club.club_service._settings", _fake_settings)
    monkeypatch.setattr("app.services.club.club_service.check_club_creation_eligibility", lambda db, uid: None)
    monkeypatch.setattr("app.services.club.club_service.validate_club_name", lambda db, name, tag: None)

    key = "club-attempt-1"
    club = club_service.create_club(db_session, owner_id=owner.id, name="Test Club", tag="TST", idempotency_key=key)
    assert get_or_create_kp_account(owner.id, db_session).balance == 900

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        club_service.create_club(db_session, owner_id=owner.id, name="Test Club 2", tag="TS2", idempotency_key=key)
    assert exc_info.value.status_code == 409
    # EP was NOT charged a second time, and no second club was created for free.
    assert get_or_create_kp_account(owner.id, db_session).balance == 900


# ── admin endpoints ──────────────────────────────────────────────────────────

def test_admin_kp_economy_settings_round_trip(db_session):
    from app.routers.admin.settings import get_kp_economy_settings, update_kp_economy_settings
    from app.schemas.admin import KpEconomySettings

    _seed_full_platform_settings_row(db_session)

    body = KpEconomySettings(
        commission_percent=15,
        kp_boost_cost_7d=100, kp_boost_cost_30d=350, kp_boost_cost_90d=900,
        kp_referral_referrer_student=200, kp_referral_referrer_teacher=500, kp_referral_referrer_parent=200,
        kp_referral_referee_student=100, kp_referral_referee_teacher=300, kp_referral_referee_parent=100,
        kp_source_daily_caps={"homework": 200},
        kp_suspicious_daily_threshold=2000,
    )
    updated = update_kp_economy_settings(body, current_user={}, db=db_session)
    assert updated["commission_percent"] == 15
    assert updated["kp_source_daily_caps"] == {"homework": 200}

    fetched = get_kp_economy_settings(current_user={}, db=db_session)
    assert fetched["commission_percent"] == 15
    assert fetched["kp_suspicious_daily_threshold"] == 2000


def test_admin_suspicious_kp_velocity_lists_users_above_threshold(db_session, monkeypatch):
    from app.routers.admin.kp import admin_list_suspicious_kp_velocity

    _set_platform_settings(monkeypatch, kp_suspicious_daily_threshold=100)

    flagged = _make_profile(db_session, first_name="Flagged")
    quiet = _make_profile(db_session, first_name="Quiet")
    award_kp(flagged.id, 500, KpSource.quiz, "Beaucoup de quiz", db_session)
    award_kp(quiet.id, 10, KpSource.quiz, "Un quiz", db_session)

    result = admin_list_suspicious_kp_velocity(hours=24, current_user={}, db=db_session)

    flagged_ids = {item["user_id"] for item in result["items"]}
    assert str(flagged.id) in flagged_ids
    assert str(quiet.id) not in flagged_ids


# ── EP→DZD conversion disabled (business/EP audit — validated Model A) ─────

def test_ep_conversion_request_is_disabled():
    from fastapi import HTTPException
    from app.routers.teachers import request_withdrawal
    from app.schemas.teacher import WithdrawalRequest

    payload = WithdrawalRequest(ep_amount=100, iban="00000000000000000000", bank_holder="Test")
    with pytest.raises(HTTPException) as exc_info:
        request_withdrawal(payload, current_user={"id": str(uuid4())}, db=None)
    assert exc_info.value.status_code == 410


def test_ep_conversion_approval_is_disabled(db_session):
    from fastapi import HTTPException
    from app.models.payment import TeacherPayout
    from app.routers.admin.content import process_withdrawal
    from app.schemas.admin import WithdrawalProcessRequest

    teacher = _make_profile(db_session)
    payout = TeacherPayout(teacher_id=teacher.id, source="ep_conversion", ep_amount=100)
    db_session.add(payout)
    db_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        process_withdrawal(
            payout.id, WithdrawalProcessRequest(action="approve", dzd_amount=1000),
            current_user={"id": str(uuid4())}, db=db_session,
        )
    assert exc_info.value.status_code == 410

    # Rejecting a leftover pending request must still work (cleanup path).
    process_withdrawal(
        payout.id, WithdrawalProcessRequest(action="reject"),
        current_user={"id": str(uuid4())}, db=db_session,
    )
    assert db_session.get(TeacherPayout, payout.id).status == "rejected"
