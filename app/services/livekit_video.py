"""LiveKit client — real WebRTC video rooms for online lessons.

Security model:
- LiveKit has no separate "public/private" room flag like some providers —
  access is 100% governed by possessing a valid, signed JWT access token
  with a `room_join` grant scoped to that exact room name. No token, no
  connection — full stop, enforced by the LiveKit server itself.
- Tokens are minted here, server-side, only after the caller (in
  app/routers/classroom.py) has verified the requester is a real
  participant of that specific session and that the join window is open.
  The frontend never sees the API key/secret — only the one-shot token.
- The teacher (or admin) gets publish/subscribe + `room_admin` grants
  (mute/remove participants) baked into the token itself.
- Tokens carry a short `ttl` a bit past the session's scheduled end — even
  a leaked token stops working on its own. The "waiting room" gate is
  enforced entirely by our own backend only ever *issuing* a token once the
  join window opens — LiveKit itself has no separate knock/lobby concept.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from livekit.api import (
    AccessToken,
    CreateRoomRequest,
    LiveKitAPI,
    VideoGrants,
)

from app.config import get_settings

settings = get_settings()

# How long past a session's scheduled end a token stays valid — enough slack
# for a lesson running a little long, not indefinite.
_GRACE_MINUTES = 45
_EMPTY_ROOM_TIMEOUT_SEC = 60 * 30  # close an empty room after 30 min of no one joining


def is_configured() -> bool:
    return bool(settings.livekit_api_key and settings.livekit_api_secret and settings.livekit_url)


def room_name_for_session(session_id: str) -> str:
    return f"session-{session_id}"


def room_key_for_session(db: Any, session: Any) -> str:
    """The real "room" a TutoringSession belongs to. For group lessons every
    enrolled student has their OWN TutoringSession row (see
    app/routers/student_teachers.py) but they must all land in the SAME
    LiveKit room / WS broadcast channel — so the key is derived from the
    shared Booking.slot_id, not the individual session id. Used by both the
    REST room endpoint and the /ws/classroom/{session_id} handler
    (app/main.py) so both resolve to the same room/channel for a group."""
    from app.models.booking import Booking

    if session.booking_id:
        booking = db.get(Booking, session.booking_id)
        if booking and booking.session_type == "group" and booking.slot_id:
            return f"slot-{booking.slot_id}"
    return f"session-{session.id}"


def group_slot_id(db: Any, session: Any) -> Optional[Any]:
    """Returns the shared Booking.slot_id if `session` is part of a group
    lesson, None for individual sessions (or ones with no booking)."""
    from app.models.booking import Booking

    if session.booking_id is None:
        return None
    booking = db.get(Booking, session.booking_id)
    if booking is None or booking.session_type != "group" or booking.slot_id is None:
        return None
    return booking.slot_id


def group_sessions(db: Any, slot_id: Any) -> list:
    """All TutoringSession rows (one per enrolled student) sharing this
    group slot."""
    from sqlmodel import select

    from app.models.booking import Booking, TutoringSession

    bookings = db.exec(select(Booking).where(Booking.slot_id == slot_id)).all()
    booking_ids = [b.id for b in bookings]
    if not booking_ids:
        return []
    return db.exec(select(TutoringSession).where(TutoringSession.booking_id.in_(booking_ids))).all()


def is_session_participant(db: Any, session: Any, user_id: Any) -> bool:
    """True if user_id is the teacher, the (individual) student, or one of
    the enrolled students of session's group lesson."""
    if user_id == session.teacher_id or user_id == session.student_id:
        return True
    slot_id = group_slot_id(db, session)
    if slot_id:
        return any(s.student_id == user_id for s in group_sessions(db, slot_id))
    return False


async def get_or_create_room(
    *,
    session_id: str,
    scheduled_end_at: datetime,
    max_participants: int,
) -> Dict[str, Any]:
    """Idempotent — LiveKit's CreateRoom RPC returns the existing room as-is
    if one with this name already exists, safe to call on every join."""
    room_name = room_name_for_session(session_id)
    async with LiveKitAPI(settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret) as lkapi:
        room = await lkapi.room.create_room(CreateRoomRequest(
            name=room_name,
            empty_timeout=_EMPTY_ROOM_TIMEOUT_SEC,
            max_participants=max_participants,
        ))
    return {"name": room.name, "sid": room.sid}


def create_access_token(
    *,
    room_name: str,
    user_id: str,
    user_name: str,
    is_owner: bool,
    scheduled_end_at: datetime,
) -> str:
    """One-shot, per-user, per-room JWT. `identity` (user_id) lets LiveKit's
    participant list/webhooks be correlated back to our own profiles.id."""
    ttl = max(timedelta(minutes=5), (scheduled_end_at + timedelta(minutes=_GRACE_MINUTES)) - datetime.utcnow())
    grants = VideoGrants(
        room_join=True,
        room=room_name,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
        room_admin=is_owner,
        room_record=is_owner,
    )
    token = (
        AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity(user_id)
        .with_name(user_name)
        .with_grants(grants)
        .with_ttl(ttl)
    )
    return token.to_jwt()


async def delete_room(room_name: str) -> None:
    """Best-effort cleanup — never raises."""
    from livekit.api import DeleteRoomRequest

    try:
        async with LiveKitAPI(settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret) as lkapi:
            await lkapi.room.delete_room(DeleteRoomRequest(room=room_name))
    except Exception:
        pass
