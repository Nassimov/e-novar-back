"""Public, phone-facing endpoints for a paired second camera. No user JWT
anywhere in this file — the phone is never a platform user (see
app/dependencies.py's get_current_camera and app/core/security.py's
create_camera_jwt). Teacher-side management (create pairing, list, share,
disconnect) lives in app/routers/classroom.py instead, gated behind the
normal get_current_user.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from app.core.classroom_ws import publish_classroom_event
from app.core.security import create_camera_jwt
from app.dependencies import get_current_camera, get_db
from app.models.booking import TutoringSession
from app.models.classroom import SessionCamera
from app.models.profile import Profile
from app.models.catalog import Subject
from app.services import camera_pairing, livekit_video as lk_video

router = APIRouter(tags=["Session Camera"])


class ResolveCodeRequest(BaseModel):
    code: str


class ClaimPairingOut(BaseModel):
    camera_jwt: str
    camera_id: str
    teacher_name: str
    subject_name: Optional[str] = None


class CameraSessionOut(BaseModel):
    livekit_url: str
    room_name: str
    token: str
    camera_id: str


def _session_end_grace(session: TutoringSession) -> datetime:
    """Same window as the teacher/student LiveKit token (see
    app/services/livekit_video.py's create_access_token) — a paired phone's
    JWT and LiveKit token must not outlive what the room itself allows."""
    duration = session.duration_min or 90
    scheduled_end = session.scheduled_at + timedelta(minutes=duration)
    return scheduled_end + timedelta(minutes=45)


@router.post("/pairing/{token}/claim", response_model=ClaimPairingOut)
def claim_camera_pairing(token: str, db: Session = Depends(get_db)):
    """The phone's own claim — called right after scanning the QR (or
    resolving the manual-entry code via /pairing/resolve first). Single-use:
    a second claim of the same token always 404s (see claim_pairing's
    atomic GETDEL), so a screenshot of the QR or a leaked link is only ever
    good for the first phone that uses it."""
    record = camera_pairing.claim_pairing(token)
    if not record:
        raise HTTPException(status_code=404, detail="Ce code a expiré ou a déjà été utilisé.")

    camera = db.get(SessionCamera, UUID(record["camera_id"]))
    session = db.get(TutoringSession, UUID(record["session_id"]))
    if camera is None or session is None:
        raise HTTPException(status_code=404, detail="Session introuvable.")

    expire_at = _session_end_grace(session)
    camera_jwt = create_camera_jwt(
        camera_id=str(camera.id), session_id=str(session.id),
        room_key=camera.room_key, expire_at=expire_at,
    )

    teacher = db.get(Profile, session.teacher_id)
    subject = db.get(Subject, session.subject_id) if session.subject_id else None

    publish_classroom_event(camera.room_key, {"type": "camera_paired", "camera_id": str(camera.id)})

    return ClaimPairingOut(
        camera_jwt=camera_jwt,
        camera_id=str(camera.id),
        teacher_name=(teacher.full_name if teacher else None) or "L'enseignant",
        subject_name=subject.name if subject else None,
    )


@router.post("/pairing/resolve")
def resolve_camera_pairing_code(body: ResolveCodeRequest):
    """Manual-entry fallback for when scanning isn't practical — resolves
    the 6-digit code shown next to the QR to its underlying pairing token,
    WITHOUT consuming it (the phone still calls /pairing/{token}/claim
    next, exactly like the QR path)."""
    token = camera_pairing.resolve_code(body.code.strip())
    if not token:
        raise HTTPException(status_code=404, detail="Code invalide ou expiré.")
    return {"token": token}


@router.get("/session", response_model=CameraSessionOut)
def get_camera_session(claims: Dict[str, Any] = Depends(get_current_camera), db: Session = Depends(get_db)):
    """Mints the actual LiveKit access token for the paired camera identity
    — called once by the phone page right before it connects, never
    beforehand (no LiveKit token is minted at claim time)."""
    camera_id = claims["camera_id"]
    camera = db.get(SessionCamera, UUID(camera_id))
    if camera is None:
        raise HTTPException(status_code=404, detail="Camera not found")

    session = db.get(TutoringSession, UUID(claims["session_id"]))
    expire_at = _session_end_grace(session) if session else datetime.now(timezone.utc) + timedelta(minutes=10)

    room_name = lk_video.room_name_for_session(camera.room_key)
    token = lk_video.create_camera_access_token(
        room_name=room_name, camera_id=camera_id, name=camera.name, expire_at=expire_at,
    )
    from app.config import get_settings as _get_settings

    return CameraSessionOut(
        livekit_url=_get_settings().livekit_url, room_name=room_name, token=token, camera_id=camera_id,
    )


@router.post("/session/connected")
def confirm_camera_connected(claims: Dict[str, Any] = Depends(get_current_camera), db: Session = Depends(get_db)):
    """Called from the phone's <LiveKitRoom onConnected> — mirrors the
    teacher/student online-connect pattern (app/routers/session_validation.py):
    the DB only ever reflects a REAL, confirmed WebRTC connection, never
    just "a token was issued"."""
    camera = db.get(SessionCamera, UUID(claims["camera_id"]))
    if camera is None:
        raise HTTPException(status_code=404, detail="Camera not found")

    camera.status = "CONNECTED"
    camera.connected_at = datetime.now(timezone.utc)
    camera.updated_at = camera.connected_at
    db.add(camera)
    db.commit()
    publish_classroom_event(camera.room_key, {"type": "camera_connected", "camera_id": str(camera.id)})
    return {"status": "CONNECTED"}
