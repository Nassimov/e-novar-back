from __future__ import annotations

"""Session validation redesign (2026-09-14, migration 118): no more code
exchange (single student "valider" click instead), plus group-lesson
support (a % threshold across every enrolled student, instead of the
teacher confirming each one individually).

Uses the real service/router functions directly against an in-memory
SQLite session, same convention as test_classroom_session_workflow.py.
"""

import datetime as dt
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.admin import PlatformSettings
from app.models.booking import Booking, TutoringSession
from app.models.profile import Profile
from app.models.session_validation import SessionValidation


def _patch_settings(monkeypatch, **overrides) -> PlatformSettings:
    """PlatformSettings has several ARRAY-typed columns (e.g.
    booking_no_response_suspension_days) that SQLite can't bind a Python
    list into — get_platform_settings' normal lazy-create-on-first-access
    path INSERTs a full row and blows up under this test DB specifically
    (a pre-existing, documented limitation — see conftest.py and
    test_kp_economy.py's own _set_platform_settings, same pattern here).
    Constructing PlatformSettings(id=True, ...) as a plain in-memory object
    and monkeypatching every module-level `get_platform_settings` name that
    actually gets called in these tests' code paths sidesteps the DB
    entirely — app.routers.session_validation and app.routers.admin.
    session_validation both bind it at import time (module-level `from ...
    import ...`), so each needs its own patch target; app.services.
    session_validation's credit_session_payout re-imports it locally
    inside the function body instead, so patching the source
    (app.services.pricing.get_platform_settings) alone is enough to reach
    that one."""
    settings = PlatformSettings(id=True, **overrides)
    monkeypatch.setattr("app.services.pricing.get_platform_settings", lambda db: settings)
    monkeypatch.setattr("app.routers.session_validation.get_platform_settings", lambda db: settings)
    monkeypatch.setattr("app.routers.admin.session_validation.get_platform_settings", lambda db: settings)
    return settings


def _make_profile(db, **overrides):
    fields = {"id": uuid4(), "email": f"{uuid4()}@test.local", "first_name": "Test", "last_name": "User"}
    fields.update(overrides)
    p = Profile(**fields)
    db.add(p)
    db.commit()
    return p


def _make_booking(db, *, teacher_id, student_id, slot_id=None, amount=2000, formula="single"):
    b = Booking(
        id=uuid4(), teacher_id=teacher_id, student_id=student_id, slot_id=slot_id,
        session_type="group" if slot_id else "individual", booking_date=dt.date.today(),
        status="confirmed", amount=amount, formula=formula, payment_method="cash",
    )
    db.add(b)
    db.commit()
    return b


def _make_session(db, *, teacher_id, student_id, booking_id, scheduled_at=None):
    s = TutoringSession(
        id=uuid4(), teacher_id=teacher_id, student_id=student_id, booking_id=booking_id,
        scheduled_at=scheduled_at or dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2),
        duration_min=60, mode="online", status="scheduled",
    )
    db.add(s)
    db.commit()
    return s


def _current_user(profile, role):
    return {"id": str(profile.id), "email": profile.email, "role": role}


def _end_session(db, session, ender_profile, ender_role):
    """Mirrors app/routers/session_validation.py's end_session fan-out for
    a single (non-group) session — sets teacher_ended_at directly rather
    than going through the full endpoint (which also touches LiveKit/WS)."""
    from app.services.session_validation import get_or_create_validation

    sv = get_or_create_validation(db, session)
    sv.teacher_ended_at = dt.datetime.now(dt.timezone.utc)
    sv.status = "awaiting_student_validation"
    db.add(sv)
    db.commit()
    return sv


# ─── individual session — code-free validate ────────────────────────────────

def test_individual_session_validate_has_no_token_required(db_session, monkeypatch):
    from app.routers.session_validation import validate_session

    _patch_settings(monkeypatch)
    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    booking = _make_booking(db_session, teacher_id=teacher.id, student_id=student.id)
    session = _make_session(db_session, teacher_id=teacher.id, student_id=student.id, booking_id=booking.id)
    _end_session(db_session, session, teacher, "teacher")

    class _FakeRequest:
        client = None

    result = validate_session(
        session_id=session.id, request=_FakeRequest(),
        current_user=_current_user(student, "student"), db=db_session,
    )
    assert result["status"] == "validated"
    sv = db_session.exec(
        __import__("sqlmodel").select(SessionValidation).where(SessionValidation.session_id == session.id)
    ).first()
    assert sv.student_validated_at is not None


def test_teacher_cannot_validate_only_student_can(db_session, monkeypatch):
    from app.routers.session_validation import validate_session

    _patch_settings(monkeypatch)
    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    booking = _make_booking(db_session, teacher_id=teacher.id, student_id=student.id)
    session = _make_session(db_session, teacher_id=teacher.id, student_id=student.id, booking_id=booking.id)
    _end_session(db_session, session, teacher, "teacher")

    class _FakeRequest:
        client = None

    with pytest.raises(HTTPException) as exc:
        validate_session(
            session_id=session.id, request=_FakeRequest(),
            current_user=_current_user(teacher, "teacher"), db=db_session,
        )
    assert exc.value.status_code == 403


def test_individual_session_group_stats_is_trivially_1_of_1(db_session, monkeypatch):
    from app.services.session_validation import group_validation_stats

    settings = _patch_settings(monkeypatch)
    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    booking = _make_booking(db_session, teacher_id=teacher.id, student_id=student.id)
    session = _make_session(db_session, teacher_id=teacher.id, student_id=student.id, booking_id=booking.id)

    stats = group_validation_stats(db_session, session, settings)
    assert stats["is_group"] is False
    assert stats["total"] == 1


# ─── group session — threshold reached ──────────────────────────────────────

def _make_group(db_session, *, n_students, subject_price=2000):
    """n_students enrolled in the same group slot — a real TeacherSlot isn't
    needed for these service-level tests, just a shared Booking.slot_id
    (group_slot_id/group_sessions key off that, see app/services/
    livekit_video.py) and matching session_type='group' bookings."""
    from app.models.profile import StudentProfile

    teacher = _make_profile(db_session)
    slot_id = uuid4()
    sessions = []
    for _ in range(n_students):
        student = _make_profile(db_session)
        db_session.add(StudentProfile(user_id=student.id))
        db_session.commit()
        booking = _make_booking(db_session, teacher_id=teacher.id, student_id=student.id, slot_id=slot_id, amount=subject_price)
        session = _make_session(db_session, teacher_id=teacher.id, student_id=student.id, booking_id=booking.id)
        sv = _end_session(db_session, session, teacher, "teacher")
        sessions.append((session, sv, student))
    return teacher, sessions


def test_group_confirm_rejected_below_threshold(db_session, monkeypatch):
    from app.services.session_validation import group_confirm_and_finalize

    settings = _patch_settings(monkeypatch, trust_group_validation_threshold_percent=70)
    teacher, sessions = _make_group(db_session, n_students=5)

    # Only 1/5 (20%) validates — well under 70%.
    from app.routers.session_validation import validate_session

    class _FakeRequest:
        client = None

    validate_session(
        session_id=sessions[0][0].id, request=_FakeRequest(),
        current_user=_current_user(sessions[0][2], "student"), db=db_session,
    )

    with pytest.raises(ValueError):
        group_confirm_and_finalize(db_session, sessions[0][0], settings, actor_user_id=teacher.id)


def test_group_confirm_pays_everyone_once_threshold_met(db_session, monkeypatch):
    """4/5 (80%) validate, threshold is 70% — group-confirm should approve
    and credit payout for ALL 5, including the one who never validated
    (product decision 2026-09-14: threshold vouches for the whole class)."""
    from app.routers.session_validation import validate_session
    from app.services.session_validation import group_confirm_and_finalize

    settings = _patch_settings(
        monkeypatch, trust_group_validation_threshold_percent=70,
        trust_auto_approve_threshold=0,  # isolate the group-override path from the normal score gate
    )
    teacher, sessions = _make_group(db_session, n_students=5, subject_price=1000)

    class _FakeRequest:
        client = None

    for session, sv, student in sessions[:4]:
        validate_session(
            session_id=session.id, request=_FakeRequest(),
            current_user=_current_user(student, "student"), db=db_session,
        )

    anchor_session = sessions[0][0]
    result = group_confirm_and_finalize(db_session, anchor_session, settings, actor_user_id=teacher.id)
    db_session.commit()

    assert result["stats"]["validated"] == 4
    assert result["stats"]["total"] == 5
    assert result["stats"]["threshold_met"] is True

    for session, sv, student in sessions:
        db_session.refresh(sv)
        assert sv.status == "approved", f"expected approved, got {sv.status} (validated={sv.student_validated_at})"
        assert sv.payment_credited_at is not None

    # The one who never clicked "valider" was still paid — via the
    # override path, flagged in their own trust_score_breakdown.
    never_validated_sv = sessions[4][1]
    db_session.refresh(never_validated_sv)
    assert never_validated_sv.student_validated_at is None
    assert never_validated_sv.trust_score_breakdown.get("group_threshold_override") is True


def test_group_report_rejected_before_deadline(db_session, monkeypatch):
    from app.services.session_validation import file_group_report

    settings = _patch_settings(monkeypatch, trust_group_validation_threshold_percent=70)
    teacher, sessions = _make_group(db_session, n_students=3)

    # Nobody validated, but the window hasn't lapsed yet (_end_session sets
    # teacher_ended_at "now", and student_validation_window_hours defaults
    # to 24 — nowhere near expired).
    with pytest.raises(ValueError):
        file_group_report(db_session, sessions[0][0], settings, actor_user_id=teacher.id, actor_ip=None)


def test_group_report_filed_and_admin_approval_pays_and_strikes(db_session, monkeypatch):
    """Below threshold AND the window has lapsed: teacher can file one
    report that fans out to every still-unvalidated sibling as a
    student_validation_neglect dispute — verifies it reuses the existing
    admin approve_validation path (payout + student strike) unmodified."""
    from app.routers.admin.session_validation import approve_validation
    from app.schemas.session_validation import AdminDecisionRequest
    from app.services.session_validation import file_group_report

    settings = _patch_settings(
        monkeypatch, trust_group_validation_threshold_percent=70,
        student_validation_window_hours=1,  # short window so "now - 5h" below has already lapsed
    )
    teacher, sessions = _make_group(db_session, n_students=3, subject_price=1500)

    # Push teacher_ended_at back far enough that the (now 1h) window has
    # definitely lapsed for all three.
    for _session, sv, _student in sessions:
        sv.teacher_ended_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=5)
        db_session.add(sv)
    db_session.commit()

    admin = _make_profile(db_session)
    result = file_group_report(db_session, sessions[0][0], settings, actor_user_id=teacher.id, actor_ip="127.0.0.1")
    db_session.commit()
    assert len(result["reported_session_ids"]) == 3

    for _session, sv, student in sessions:
        db_session.refresh(sv)
        assert sv.status == "admin_review"
        assert sv.dispute_reason_code == "student_validation_neglect"

        approve_validation(
            validation_id=sv.id, body=AdminDecisionRequest(note="Vérifié manuellement"),
            admin=_current_user(admin, "admin"), db=db_session,
        )
        db_session.commit()
        db_session.refresh(sv)
        assert sv.status == "approved"
        assert sv.payment_credited_at is not None

        from app.models.profile import StudentProfile
        sp = db_session.get(StudentProfile, student.id)
        assert sp is not None and sp.no_show_strikes == 1


def test_group_report_rejected_if_already_filed(db_session, monkeypatch):
    from app.services.session_validation import file_group_report

    settings = _patch_settings(
        monkeypatch, trust_group_validation_threshold_percent=70, student_validation_window_hours=1,
    )
    teacher, sessions = _make_group(db_session, n_students=2)
    for _session, sv, _student in sessions:
        sv.teacher_ended_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=5)
        db_session.add(sv)
    db_session.commit()

    file_group_report(db_session, sessions[0][0], settings, actor_user_id=teacher.id, actor_ip=None)
    db_session.commit()

    with pytest.raises(ValueError):
        file_group_report(db_session, sessions[0][0], settings, actor_user_id=teacher.id, actor_ip=None)


def test_notify_accepts_plain_string_without_crashing(db_session):
    """Regression test for a real pre-existing bug found while working on
    this file: _notify's signature always documented title_i18n/body_i18n
    as dicts, but several call sites (app/routers/session_validation.py's
    end_session, dispute_session) passed plain strings — _render_i18n does
    `title_i18n.get(lang)`, which raised AttributeError on a plain str,
    silently swallowed by notification_engine.emit()'s own broad except,
    meaning those specific notifications had been sending nothing at all.
    _notify now normalizes a bare string into a same-text dict."""
    from app.services.session_validation import _notify

    student = _make_profile(db_session)
    # Must not raise — the whole point of the fix.
    _notify(db_session, student.id, "Plain title", "Plain body", {"x": "y"})
