from __future__ import annotations

"""
2FA-gated sign-in (2026-09-18 request): POST /signin must hand back a
SignInTotpChallenge instead of real tokens once a user has TOTP enabled,
and POST /signin/totp must only issue the real (already-authenticated,
stashed) session after a correct code. This is the highest-risk change in
this batch — it sits on the main login path every user goes through — so
it gets its own dedicated coverage, run directly against the real router
functions (no network I/O: Supabase and Redis are faked).
"""

import json
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pyotp
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.models.profile import Profile
from app.routers import auth as auth_router
from app.schemas.auth import SignInRequest, SignInTotpVerifyRequest


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    def setex(self, key, ttl, value):
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        return 1 if self.store.pop(key, None) is not None else 0


def _make_profile(db, **overrides):
    fields = {"id": uuid4(), "email": f"{uuid4()}@example.com", "first_name": "Test", "last_name": "User"}
    fields.update(overrides)
    p = Profile(**fields)
    db.add(p)
    db.commit()
    return p


def _fake_supabase_result(user_id: str, email: str, role: str = "student"):
    supabase_user = MagicMock()
    supabase_user.id = user_id
    supabase_user.email = email
    supabase_user.app_metadata = {"role": role}
    supabase_user.user_metadata = {"full_name": "Test User"}
    session = MagicMock()
    session.access_token = "real-access-token"
    session.refresh_token = "real-refresh-token"
    result = MagicMock()
    result.user = supabase_user
    result.session = session
    return result


def _request():
    scope = {"type": "http", "method": "POST", "headers": [], "path": "/api/auth/signin", "query_string": b"", "client": ("127.0.0.1", 0)}
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    return Request(scope, receive)


def test_signin_without_2fa_returns_tokens_directly(db_session):
    profile = _make_profile(db_session)
    fake_redis = FakeRedis()

    with patch.object(auth_router.auth_service, "login_with_supabase",
                       return_value=_fake_supabase_result(str(profile.id), profile.email)), \
         patch("app.core.redis.get_redis_client", return_value=fake_redis), \
         patch("app.services.onesignal.register_user"):
        payload = SignInRequest(email=profile.email, password="whatever")
        result = auth_router.signin(payload, _request(), db_session)

    from app.schemas.auth import TokenResponse
    assert isinstance(result, TokenResponse)
    assert result.access_token == "real-access-token"
    assert result.user.totp_enabled is False


def test_signin_with_2fa_returns_challenge_not_tokens(db_session):
    secret = pyotp.random_base32()
    profile = _make_profile(db_session, totp_enabled=True, totp_secret=secret)
    fake_redis = FakeRedis()

    with patch.object(auth_router.auth_service, "login_with_supabase",
                       return_value=_fake_supabase_result(str(profile.id), profile.email)), \
         patch("app.core.redis.get_redis_client", return_value=fake_redis), \
         patch("app.services.onesignal.register_user"):
        payload = SignInRequest(email=profile.email, password="whatever")
        result = auth_router.signin(payload, _request(), db_session)

        from app.schemas.auth import SignInTotpChallenge
        assert isinstance(result, SignInTotpChallenge)
        assert result.totp_required is True
        challenge_token = result.challenge_token

        # Step 2 with the correct code hands back the real, already-issued session.
        code = pyotp.TOTP(secret).now()
        final = auth_router.signin_totp(
            SignInTotpVerifyRequest(challenge_token=challenge_token, totp_code=code),
            _request(), db_session,
        )
    assert final.access_token == "real-access-token"
    assert final.user.totp_enabled is True


def test_signin_totp_wrong_code_rejected_and_challenge_survives(db_session):
    secret = pyotp.random_base32()
    profile = _make_profile(db_session, totp_enabled=True, totp_secret=secret)
    fake_redis = FakeRedis()

    with patch.object(auth_router.auth_service, "login_with_supabase",
                       return_value=_fake_supabase_result(str(profile.id), profile.email)), \
         patch("app.core.redis.get_redis_client", return_value=fake_redis), \
         patch("app.services.onesignal.register_user"):
        payload = SignInRequest(email=profile.email, password="whatever")
        challenge = auth_router.signin(payload, _request(), db_session)

        with pytest.raises(HTTPException) as exc:
            auth_router.signin_totp(
                SignInTotpVerifyRequest(challenge_token=challenge.challenge_token, totp_code="000000"),
                _request(), db_session,
            )
        assert exc.value.status_code == 401

        # Challenge is still usable — a wrong code must not consume it (lets
        # the user retry within the same 5-minute window).
        code = pyotp.TOTP(secret).now()
        final = auth_router.signin_totp(
            SignInTotpVerifyRequest(challenge_token=challenge.challenge_token, totp_code=code),
            _request(), db_session,
        )
    assert final.access_token == "real-access-token"


def test_signin_totp_expired_or_unknown_challenge_rejected(db_session):
    fake_redis = FakeRedis()
    with patch("app.core.redis.get_redis_client", return_value=fake_redis):
        with pytest.raises(HTTPException) as exc:
            auth_router.signin_totp(
                SignInTotpVerifyRequest(challenge_token="does-not-exist", totp_code="123456"),
                _request(), db_session,
            )
    assert exc.value.status_code == 401
