from __future__ import annotations

"""
Account-security endpoints (app/routers/account_security.py) — real
password change, real TOTP 2FA, and scheduled/cancelable account deletion
with its business-rule guardrails (2026-09-18 request: these settings-page
features were previously 100% mocked in the frontend with zero backend
support at all).

Same convention as tests/test_classroom_session_workflow.py — real router
functions called directly as plain Python against an in-memory SQLite
session; Supabase calls are monkeypatched (never real network I/O).
"""

import datetime as dt
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.models.booking import Booking
from app.models.payment import TeacherPayout
from app.models.profile import Profile, TeacherProfile
from app.routers import account_security as acct


def _make_profile(db, **overrides):
    fields = {
        "id": uuid4(), "email": f"{uuid4()}@test.local",
        "first_name": "Test", "last_name": "User",
    }
    fields.update(overrides)
    p = Profile(**fields)
    db.add(p)
    db.commit()
    return p


def _make_teacher(db, **overrides):
    profile = _make_profile(db)
    tp = TeacherProfile(user_id=profile.id, status="approved", verified=True, **overrides)
    db.add(tp)
    db.commit()
    return profile, tp


def _current_user(profile, role="student"):
    return {"id": str(profile.id), "email": profile.email, "role": role, "claims": {}}


def _creds():
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials="fake-access-token")


# ── delete: business-rule guardrails ────────────────────────────────────────

def test_student_with_upcoming_booking_cannot_delete(db_session):
    profile = _make_profile(db_session)
    teacher, _tp = _make_teacher(db_session)
    db_session.add(Booking(
        student_id=profile.id, teacher_id=teacher.id, booking_date=dt.date.today(), status="confirmed",
    ))
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        acct.schedule_account_deletion(_creds(), _current_user(profile, "student"), db_session)
    assert exc.value.status_code == 409
    assert "upcoming_booking" in exc.value.detail["reasons"]


def test_teacher_with_wallet_balance_cannot_delete(db_session):
    profile, tp = _make_teacher(db_session, wallet_balance_dzd=5000)

    with pytest.raises(HTTPException) as exc:
        acct.schedule_account_deletion(_creds(), _current_user(profile, "teacher"), db_session)
    assert exc.value.status_code == 409
    assert "wallet_balance" in exc.value.detail["reasons"]


def test_teacher_with_pending_payout_cannot_delete(db_session):
    profile, _tp = _make_teacher(db_session)
    db_session.add(TeacherPayout(teacher_id=profile.id, source="wallet", dzd_amount=1000, status="pending"))
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        acct.schedule_account_deletion(_creds(), _current_user(profile, "teacher"), db_session)
    assert exc.value.status_code == 409
    assert "pending_payout" in exc.value.detail["reasons"]


def test_clean_account_can_schedule_and_cancel_deletion(db_session):
    profile = _make_profile(db_session)

    with patch("app.database.get_supabase_service") as mock_supa:
        mock_supa.return_value.auth.admin.sign_out = MagicMock()
        out = acct.schedule_account_deletion(_creds(), _current_user(profile, "student"), db_session)

    assert out.deletion_scheduled_for
    db_session.refresh(profile)
    assert profile.deletion_scheduled_for is not None
    assert profile.deletion_requested_at is not None
    # ~60 days out
    delta = profile.deletion_scheduled_for - profile.deletion_requested_at
    assert 59 <= delta.days <= 60

    acct.cancel_account_deletion(_current_user(profile, "student"), db_session)
    db_session.refresh(profile)
    assert profile.deletion_scheduled_for is None
    assert profile.deletion_requested_at is None


def test_scheduling_deletion_twice_is_idempotent(db_session):
    """Calling /delete again while already scheduled must not push the date
    further out (that would make the 60-day promise meaningless)."""
    profile = _make_profile(db_session)
    with patch("app.database.get_supabase_service") as mock_supa:
        mock_supa.return_value.auth.admin.sign_out = MagicMock()
        first = acct.schedule_account_deletion(_creds(), _current_user(profile, "student"), db_session)
        second = acct.schedule_account_deletion(_creds(), _current_user(profile, "student"), db_session)
    assert first.deletion_scheduled_for == second.deletion_scheduled_for


# ── 2FA (TOTP) ───────────────────────────────────────────────────────────────

def test_totp_setup_confirm_enables_2fa(db_session):
    profile = _make_profile(db_session)
    setup = acct.setup_totp(_current_user(profile), db_session)
    assert setup["secret"]
    db_session.refresh(profile)
    assert profile.totp_secret == setup["secret"]
    assert profile.totp_enabled is False

    import pyotp
    code = pyotp.TOTP(setup["secret"]).now()
    result = acct.confirm_totp(acct.TotpConfirmRequest(code=code), _current_user(profile), db_session)
    assert result["totp_enabled"] is True
    db_session.refresh(profile)
    assert profile.totp_enabled is True


def test_totp_confirm_wrong_code_rejected(db_session):
    profile = _make_profile(db_session)
    acct.setup_totp(_current_user(profile), db_session)

    with pytest.raises(HTTPException) as exc:
        acct.confirm_totp(acct.TotpConfirmRequest(code="000000"), _current_user(profile), db_session)
    assert exc.value.status_code == 401
    db_session.refresh(profile)
    assert profile.totp_enabled is False


def test_totp_disable_requires_correct_password(db_session):
    profile = _make_profile(db_session)
    profile.totp_enabled = True
    profile.totp_secret = "JBSWY3DPEHPK3PXP"
    db_session.add(profile)
    db_session.commit()

    with patch.object(acct.auth_service, "login_with_supabase", side_effect=Exception("bad creds")):
        with pytest.raises(HTTPException) as exc:
            acct.disable_totp(acct.TotpDisableRequest(password="wrong"), _current_user(profile), db_session)
        assert exc.value.status_code == 401

    with patch.object(acct.auth_service, "login_with_supabase", return_value=MagicMock()):
        result = acct.disable_totp(acct.TotpDisableRequest(password="right"), _current_user(profile), db_session)
    assert result["totp_enabled"] is False
    db_session.refresh(profile)
    assert profile.totp_enabled is False
    assert profile.totp_secret is None


# ── password change ──────────────────────────────────────────────────────────

def test_change_password_requires_correct_current_password(db_session):
    import asyncio
    from starlette.requests import Request

    profile = _make_profile(db_session)

    async def _run(current_password_ok: bool):
        scope = {"type": "http", "method": "POST", "headers": [], "path": "/api/account/password", "query_string": b"", "client": ("127.0.0.1", 0)}
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        request = Request(scope, receive)
        side_effect = (lambda *a, **k: MagicMock()) if current_password_ok else (lambda *a, **k: (_ for _ in ()).throw(Exception("bad")))
        with patch.object(acct.auth_service, "login_with_supabase", side_effect=side_effect), \
             patch("app.database.get_supabase_service") as mock_supa:
            mock_supa.return_value.auth.admin.update_user_by_id = MagicMock()
            return acct.change_password(
                acct.ChangePasswordRequest(current_password="x", new_password="newpassword123"),
                request, _current_user(profile), db_session,
            )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(_run(False))
    assert exc.value.status_code == 401

    result = asyncio.run(_run(True))
    assert result == {"message": "Password updated"}
