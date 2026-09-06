from __future__ import annotations

"""Second-camera pairing (docs/migrations/108_session_cameras.sql) — unit
tests for the Redis pairing flow + the camera-scoped JWT, and integration
tests for the teacher-only management endpoints, the public phone-facing
endpoints, and the session-end cleanup path. Mirrors
test_classroom_session_workflow.py's style: direct router-function calls
against an in-memory SQLite db_session, no real Redis/LiveKit — both faked.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.booking import TutoringSession
from app.models.classroom import SessionCamera
from app.models.profile import Profile


def _make_profile(db_session, **overrides):
    fields = {"id": uuid4(), "email": f"{uuid4()}@test.local", "first_name": "Test", "last_name": "User"}
    fields.update(overrides)
    profile = Profile(**fields)
    db_session.add(profile)
    db_session.commit()
    return profile


def _make_session(db_session, *, teacher_id, student_id, scheduled_at=None, **overrides):
    fields = {
        "id": uuid4(), "teacher_id": teacher_id, "student_id": student_id,
        "scheduled_at": scheduled_at or datetime.now(timezone.utc), "duration_min": 60,
        "mode": "online", "status": "scheduled",
    }
    fields.update(overrides)
    session = TutoringSession(**fields)
    db_session.add(session)
    db_session.commit()
    return session


def _current_user(profile: Profile, role: str = "student") -> dict:
    return {"id": str(profile.id), "email": profile.email, "role": role}


class FakeRedis:
    """Minimal in-memory stand-in for the handful of redis-py calls
    app/services/camera_pairing.py and get_current_camera actually make —
    TTL (`ex=`) isn't simulated, an "expired" pairing is modeled by
    deleting the key directly instead of waiting."""

    def __init__(self):
        self.store: dict[str, str] = {}

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def getdel(self, key):
        return self.store.pop(key, None)

    def delete(self, key):
        return 1 if self.store.pop(key, None) is not None else 0


@pytest.fixture
def fake_redis():
    return FakeRedis()


# ─── Pairing service (Redis) ────────────────────────────────────────────────

def test_create_pairing_generates_single_use_token_and_code(fake_redis):
    from app.services import camera_pairing

    with patch("app.services.camera_pairing.get_redis_client", return_value=fake_redis):
        pairing = camera_pairing.create_pairing(
            camera_id="cam-1", session_id="sess-1", teacher_id="teach-1", room_key="session-sess-1",
        )
        assert len(pairing["code"]) == 6
        assert pairing["token"]

        record = camera_pairing.claim_pairing(pairing["token"])
        assert record == {
            "camera_id": "cam-1", "session_id": "sess-1", "teacher_id": "teach-1", "room_key": "session-sess-1",
        }


def test_claim_pairing_is_single_use(fake_redis):
    from app.services import camera_pairing

    with patch("app.services.camera_pairing.get_redis_client", return_value=fake_redis):
        pairing = camera_pairing.create_pairing(
            camera_id="cam-1", session_id="sess-1", teacher_id="teach-1", room_key="session-sess-1",
        )
        assert camera_pairing.claim_pairing(pairing["token"]) is not None
        # Second claim of the SAME token — replay, or two devices scanning
        # the same QR — must always fail.
        assert camera_pairing.claim_pairing(pairing["token"]) is None


def test_claim_pairing_rejects_unknown_or_expired_token(fake_redis):
    from app.services import camera_pairing

    with patch("app.services.camera_pairing.get_redis_client", return_value=fake_redis):
        assert camera_pairing.claim_pairing("never-issued-token") is None


def test_resolve_code_does_not_consume_the_pairing(fake_redis):
    from app.services import camera_pairing

    with patch("app.services.camera_pairing.get_redis_client", return_value=fake_redis):
        pairing = camera_pairing.create_pairing(
            camera_id="cam-1", session_id="sess-1", teacher_id="teach-1", room_key="session-sess-1",
        )
        resolved = camera_pairing.resolve_code(pairing["code"])
        assert resolved == pairing["token"]
        # Resolving the code is read-only — the token itself must still be claimable.
        assert camera_pairing.claim_pairing(pairing["token"]) is not None


def test_resolve_code_rejects_unknown_code(fake_redis):
    from app.services import camera_pairing

    with patch("app.services.camera_pairing.get_redis_client", return_value=fake_redis):
        assert camera_pairing.resolve_code("000000") is None


def test_revoke_camera_blocks_get_current_camera(fake_redis):
    from app.core.security import create_camera_jwt
    from app.dependencies import get_current_camera
    from app.services import camera_pairing

    token = create_camera_jwt(
        camera_id="cam-1", session_id="sess-1", room_key="session-sess-1",
        expire_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    creds = type("Creds", (), {"credentials": token})()

    with patch("app.dependencies.get_redis_client", return_value=fake_redis), \
         patch("app.services.camera_pairing.get_redis_client", return_value=fake_redis):
        claims = get_current_camera(creds)
        assert claims["camera_id"] == "cam-1"

        camera_pairing.revoke_camera("cam-1")
        with pytest.raises(HTTPException) as exc:
            get_current_camera(creds)
        assert exc.value.status_code == 401


# ─── Camera-scoped JWT ───────────────────────────────────────────────────────

def test_camera_jwt_is_rejected_by_admin_and_supabase_decoders():
    """A camera token must never be usable anywhere a user/admin JWT is
    expected — see app/core/security.py's `type` claim discriminator."""
    from app.core.security import create_camera_jwt, decode_admin_jwt, decode_supabase_jwt

    token = create_camera_jwt(
        camera_id="cam-1", session_id="sess-1", room_key="session-sess-1",
        expire_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert decode_admin_jwt(token) is None
    # decode_supabase_jwt doesn't check `type` (Supabase tokens don't carry
    # one), so this only proves the signature itself doesn't verify against
    # the Supabase secret — the two are signed with different keys.
    assert decode_supabase_jwt(token) is None


# ─── Teacher-only management endpoints ──────────────────────────────────────

@pytest.mark.asyncio
async def test_create_camera_requires_teacher(db_session, fake_redis):
    from app.routers.classroom import create_camera

    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    session = _make_session(db_session, teacher_id=teacher.id, student_id=student.id)

    with patch("app.services.camera_pairing.get_redis_client", return_value=fake_redis):
        with pytest.raises(HTTPException) as exc:
            create_camera(session.id, _current_user(student), db_session)
        assert exc.value.status_code == 403

        result = create_camera(session.id, _current_user(teacher, role="teacher"), db_session)
        assert result.camera.status == "CONNECTING"
        assert result.pairing_code
        assert f"/camera/{result.pairing_token}" in result.join_url


@pytest.mark.asyncio
async def test_list_cameras_is_readable_by_student(db_session, fake_redis):
    from app.routers.classroom import create_camera, list_cameras

    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    session = _make_session(db_session, teacher_id=teacher.id, student_id=student.id)

    with patch("app.services.camera_pairing.get_redis_client", return_value=fake_redis):
        create_camera(session.id, _current_user(teacher, role="teacher"), db_session)

    cameras = list_cameras(session.id, _current_user(student), db_session)
    assert len(cameras) == 1


@pytest.mark.asyncio
async def test_share_and_stop_sharing_require_teacher(db_session):
    from app.routers.classroom import share_camera, stop_sharing_camera

    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    session = _make_session(db_session, teacher_id=teacher.id, student_id=student.id)
    camera = SessionCamera(
        session_id=session.id, teacher_id=teacher.id, room_key=f"session-{session.id}", status="CONNECTED",
    )
    db_session.add(camera)
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        await share_camera(session.id, camera.id, _current_user(student), db_session)
    assert exc.value.status_code == 403

    with patch("app.services.livekit_video.set_camera_track_shared", new=AsyncMock(return_value=True)):
        result = await share_camera(session.id, camera.id, _current_user(teacher, role="teacher"), db_session)
    assert result.is_shared is True

    with pytest.raises(HTTPException) as exc:
        await stop_sharing_camera(session.id, camera.id, _current_user(student), db_session)
    assert exc.value.status_code == 403

    with patch("app.services.livekit_video.set_camera_track_shared", new=AsyncMock(return_value=True)):
        result = await stop_sharing_camera(session.id, camera.id, _current_user(teacher, role="teacher"), db_session)
    assert result.is_shared is False


@pytest.mark.asyncio
async def test_disconnect_camera_requires_teacher_and_revokes(db_session, fake_redis):
    from app.routers.classroom import disconnect_camera

    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    session = _make_session(db_session, teacher_id=teacher.id, student_id=student.id)
    camera = SessionCamera(
        session_id=session.id, teacher_id=teacher.id, room_key=f"session-{session.id}", status="CONNECTED",
    )
    db_session.add(camera)
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        await disconnect_camera(session.id, camera.id, _current_user(student), db_session)
    assert exc.value.status_code == 403

    with patch("app.services.livekit_video.remove_camera_participant", new=AsyncMock()), \
         patch("app.services.camera_pairing.get_redis_client", return_value=fake_redis):
        await disconnect_camera(session.id, camera.id, _current_user(teacher, role="teacher"), db_session)

    db_session.refresh(camera)
    assert camera.status == "DISCONNECTED"
    assert camera.is_shared is False
    assert camera.disconnected_at is not None


# ─── Public phone-facing endpoints ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_claim_camera_pairing_end_to_end(db_session, fake_redis):
    from app.routers.camera_pairing import claim_camera_pairing
    from app.services import camera_pairing

    teacher = _make_profile(db_session, first_name="Yasmine")
    student = _make_profile(db_session)
    session = _make_session(db_session, teacher_id=teacher.id, student_id=student.id)
    camera = SessionCamera(session_id=session.id, teacher_id=teacher.id, room_key=f"session-{session.id}")
    db_session.add(camera)
    db_session.commit()

    with patch("app.services.camera_pairing.get_redis_client", return_value=fake_redis):
        pairing = camera_pairing.create_pairing(
            camera_id=str(camera.id), session_id=str(session.id),
            teacher_id=str(teacher.id), room_key=camera.room_key,
        )
        result = claim_camera_pairing(pairing["token"], db_session)
        assert result.camera_id == str(camera.id)
        assert result.camera_jwt

        # Reusing the same (now-consumed) token must 404, never re-issue a token.
        with pytest.raises(HTTPException) as exc:
            claim_camera_pairing(pairing["token"], db_session)
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_claim_camera_pairing_rejects_invalid_token(db_session, fake_redis):
    from app.routers.camera_pairing import claim_camera_pairing

    with patch("app.services.camera_pairing.get_redis_client", return_value=fake_redis):
        with pytest.raises(HTTPException) as exc:
            claim_camera_pairing("not-a-real-token", db_session)
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_camera_session_mints_video_only_token(db_session):
    from app.routers.camera_pairing import get_camera_session

    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    session = _make_session(db_session, teacher_id=teacher.id, student_id=student.id)
    camera = SessionCamera(session_id=session.id, teacher_id=teacher.id, room_key=f"session-{session.id}")
    db_session.add(camera)
    db_session.commit()

    claims = {"camera_id": str(camera.id), "session_id": str(session.id), "room_key": camera.room_key}
    with patch("app.services.livekit_video.create_camera_access_token", return_value="fake.camera.jwt") as mint:
        result = get_camera_session(claims, db_session)
    assert result.token == "fake.camera.jwt"
    # Never granted subscribe/data/admin — see create_camera_access_token itself,
    # this just pins that the router passes the camera's own id/name through.
    assert mint.call_args.kwargs["camera_id"] == str(camera.id)


@pytest.mark.asyncio
async def test_confirm_camera_connected_sets_connected_status(db_session):
    from app.routers.camera_pairing import confirm_camera_connected

    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    session = _make_session(db_session, teacher_id=teacher.id, student_id=student.id)
    camera = SessionCamera(session_id=session.id, teacher_id=teacher.id, room_key=f"session-{session.id}")
    db_session.add(camera)
    db_session.commit()

    confirm_camera_connected({"camera_id": str(camera.id)}, db_session)

    db_session.refresh(camera)
    assert camera.status == "CONNECTED"
    assert camera.connected_at is not None


# ─── Cleanup on session end ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_end_session_disconnects_active_cameras(db_session, fake_redis):
    """A paired phone has no user JWT and never sees the classroom WS
    session_ended event (see app/routers/camera_pairing.py) — it only finds
    out because end_session force-disconnects it from LiveKit. Pins that
    this cleanup actually runs, not just the validation-status flip."""
    from app.routers.session_validation import end_session

    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    session = _make_session(
        db_session, teacher_id=teacher.id, student_id=student.id,
        scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=90),
    )
    room_key = f"session-{session.id}"
    camera = SessionCamera(session_id=session.id, teacher_id=teacher.id, room_key=room_key, status="CONNECTED", is_shared=True)
    already_gone = SessionCamera(
        session_id=session.id, teacher_id=teacher.id, room_key=room_key, status="DISCONNECTED",
    )
    db_session.add(camera)
    db_session.add(already_gone)
    db_session.commit()

    fake_request = type("Req", (), {"client": type("Client", (), {"host": "127.0.0.1"})()})()

    with patch("app.services.livekit_video.remove_camera_participant", new=AsyncMock()) as remove_mock, \
         patch("app.services.camera_pairing.get_redis_client", return_value=fake_redis), \
         patch("app.services.camera_pairing.revoke_camera") as revoke_mock:
        await end_session(session.id, fake_request, _current_user(teacher, role="teacher"), db_session)

    db_session.refresh(camera)
    assert camera.status == "DISCONNECTED"
    assert camera.is_shared is False
    assert camera.disconnected_at is not None
    remove_mock.assert_awaited_once()
    revoke_mock.assert_called_once_with(str(camera.id))
