"""Online classroom — real video room (LiveKit), chapters, files, live
quizzes, and in-call chat provisioning. See app/services/livekit_video.py
for the video security model.

Individual vs group: a session's `Booking.session_type` decides which. For
group lessons, every enrolled student has their OWN Booking+TutoringSession
row (see app/routers/student_teachers.py) but they must all land in the
SAME LiveKit room — so the room key is derived from the shared
`Booking.slot_id` for group sessions, and from the session's own id for
individual ones. Same logic for the shared group chat conversation.
"""
from __future__ import annotations

import io
import logging
import posixpath
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlmodel import Session, select

from app.core.classroom_ws import publish_classroom_event
from app.dependencies import get_current_user, get_db
from app.models.booking import Booking, TutoringSession
from app.models.catalog import Level, Subject
from app.models.classroom import (
    SessionBookmark,
    SessionChapter,
    SessionFile,
    SessionNotepad,
    SessionQuiz,
    SessionQuizAnswer,
    SessionRecording,
    SessionWhiteboardSnapshot,
    SessionWhiteboardState,
)
from app.models.conversation import ChatMessage, Conversation, ConversationParticipant
from app.models.enums import KpSource
from app.models.profile import Profile
from app.models.scheduling import TeacherSlot
from app.services import egress as lk_egress
from app.services import livekit_video as lk_video
from app.services.kp import award_kp
from app.services.pricing import get_platform_settings
from app.services.storage import upload_file

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Classroom"])

QUIZ_CORRECT_ANSWER_EP = 10


# ─── Helpers ────────────────────────────────────────────────────────────────

def _load_session(db: Session, session_id: UUID) -> TutoringSession:
    session = db.get(TutoringSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


def _authorize(session: TutoringSession, current_user: Dict[str, Any]) -> UUID:
    uid = UUID(current_user["id"])
    role = current_user.get("role", "student")
    if uid != session.student_id and uid != session.teacher_id and role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    return uid


def _group_slot_id(db: Session, session: TutoringSession) -> Optional[UUID]:
    return lk_video.group_slot_id(db, session)


def _group_sessions(db: Session, slot_id: UUID) -> List[TutoringSession]:
    return lk_video.group_sessions(db, slot_id)


def _group_session_ids(db: Session, session: TutoringSession) -> List[UUID]:
    """Every TutoringSession id sharing this session's live-classroom room —
    just [session.id] for an individual lesson, every enrolled student's own
    row for a group one. Live quizzes are dispatched/broadcast class-wide
    (see quiz_dispatched's room_key-based publish below) from whichever
    single session_id the teacher's own view happens to be bound to, so
    reading them back must resolve across the whole group too — otherwise a
    student whose own session_id differs from the one the teacher used never
    sees a quiz that was, from the teacher's side, clearly sent."""
    slot_id = _group_slot_id(db, session)
    if not slot_id:
        return [session.id]
    return [s.id for s in _group_sessions(db, slot_id)]


def _display_name(profile: Optional[Profile]) -> str:
    return (profile.full_name if profile else None) or "Utilisateur"


# ─── Video room ─────────────────────────────────────────────────────────────

class RoomParticipant(BaseModel):
    user_id: str
    name: str
    avatar_url: Optional[str] = None
    role: str  # 'teacher' | 'student'


class RoomResponse(BaseModel):
    livekit_url: str
    room_name: str
    token: str
    is_owner: bool
    session_type: str
    mode: str
    scheduled_at: str
    scheduled_end_at: str
    duration_min: int
    subject_name: Optional[str] = None
    level_name: Optional[str] = None
    can_join: bool
    join_opens_at: str
    participants: List[RoomParticipant]


@router.get("/{session_id}/room", response_model=RoomResponse)
async def get_classroom_room(
    session_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = _load_session(db, session_id)
    uid = _authorize(session, current_user)
    role = current_user.get("role", "student")

    if session.mode != "online":
        raise HTTPException(status_code=400, detail="Cette séance n'est pas une séance en ligne.")
    if session.status in ("cancelled", "no_show"):
        raise HTTPException(status_code=409, detail=f"Séance en statut '{session.status}' — impossible de rejoindre.")

    settings = get_platform_settings(db)
    duration = session.duration_min or 90
    scheduled_end = session.scheduled_at + timedelta(minutes=duration)
    join_opens_at = session.scheduled_at - timedelta(minutes=settings.token_visible_minutes_before)
    now = datetime.now(timezone.utc)
    grace_end = scheduled_end + timedelta(minutes=45)

    # Once either participant has formally ended the session (the "Terminer
    # la séance" action, distinct from just disconnecting/hanging up), the
    # room is no longer rejoinable regardless of the time window — this is
    # what actually enforces "tant qu'un des deux n'a pas mis fin à la
    # séance, on peut la rejoindre" from the other side: ending it is what
    # closes that door, not merely leaving the call.
    from app.services.session_validation import get_or_create_validation as _get_or_create_validation
    sv = _get_or_create_validation(db, session)
    db.commit()
    can_join = (join_opens_at <= now <= grace_end) and sv.status == "scheduled"
    if not can_join:
        return RoomResponse(
            livekit_url="", room_name="", token="", is_owner=False,
            session_type="group" if _group_slot_id(db, session) else "individual",
            mode=session.mode,
            scheduled_at=session.scheduled_at.isoformat(),
            scheduled_end_at=scheduled_end.isoformat(),
            duration_min=duration,
            can_join=False,
            join_opens_at=join_opens_at.isoformat(),
            participants=[],
        )

    booking = db.get(Booking, session.booking_id) if session.booking_id else None
    slot_id = _group_slot_id(db, session)

    room_key = lk_video.room_key_for_session(db, session)
    if slot_id:
        group_sessions = _group_sessions(db, slot_id)
        slot = db.get(TeacherSlot, slot_id)
        max_participants = max(2, (slot.max_students if slot else len(group_sessions)) + 1)
        student_ids = list({s.student_id for s in group_sessions})
        session_type = "group"
    else:
        max_participants = 2
        student_ids = [session.student_id]
        session_type = "individual"

    room = await lk_video.get_or_create_room(
        session_id=room_key, scheduled_end_at=scheduled_end, max_participants=max_participants,
    )

    is_owner = uid == session.teacher_id or role == "admin"
    my_profile = db.exec(select(Profile).where(Profile.id == uid)).first()
    token = lk_video.create_access_token(
        room_name=room["name"], user_id=str(uid), user_name=_display_name(my_profile),
        is_owner=is_owner, scheduled_end_at=scheduled_end,
    )

    teacher_profile = db.exec(select(Profile).where(Profile.id == session.teacher_id)).first()
    student_profiles = db.exec(select(Profile).where(Profile.id.in_(student_ids))).all()
    profile_map = {p.id: p for p in student_profiles}

    participants = [RoomParticipant(
        user_id=str(session.teacher_id), name=_display_name(teacher_profile),
        avatar_url=teacher_profile.avatar_url if teacher_profile else None, role="teacher",
    )]
    for sid in student_ids:
        p = profile_map.get(sid)
        participants.append(RoomParticipant(
            user_id=str(sid), name=_display_name(p), avatar_url=p.avatar_url if p else None, role="student",
        ))

    subject_name = None
    if session.subject_id:
        subj = db.get(Subject, session.subject_id)
        subject_name = subj.name if subj else None
    level_name = None
    if session.level_id:
        lvl = db.get(Level, session.level_id)
        level_name = lvl.label if lvl else None

    now_join = datetime.now(timezone.utc)
    # NOTE: teacher_joined_at/student_joined_at are deliberately NOT set
    # here. This endpoint is fetched by the waiting-room page's 15s poll —
    # merely fetching room/token info is not the same as actually joining
    # the LiveKit call, and the online no-show detector (app/workers/
    # booking_tasks.py) needs the real thing: those fields are set by
    # POST /{session_id}/validation/online-connect instead, which only
    # fires from the live page's LiveKitRoom onConnected callback — i.e.
    # once WebRTC has genuinely connected.
    if session.status == "scheduled":
        session.status = "live"
        if session.started_at is None:
            session.started_at = now_join
        db.add(session)
    db.commit()

    from app.config import get_settings as _get_settings
    return RoomResponse(
        livekit_url=_get_settings().livekit_url, room_name=room["name"], token=token, is_owner=is_owner,
        session_type=session_type, mode=session.mode, scheduled_at=session.scheduled_at.isoformat(),
        scheduled_end_at=scheduled_end.isoformat(), duration_min=duration,
        subject_name=subject_name, level_name=level_name, can_join=True,
        join_opens_at=join_opens_at.isoformat(), participants=participants,
    )


@router.post("/{session_id}/mute/{target_user_id}")
async def mute_participant(
    session_id: UUID,
    target_user_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Teacher moderation control — mute a student's microphone mid-session.
    Server-enforced via the LiveKit room API (the student's own client
    can't refuse it), not a request the student can just ignore. Also
    broadcasts a `participant_muted` classroom event so the muted
    student's own UI reflects it (their local mic toggle shows muted) and
    everyone else sees who was muted, without needing to poll LiveKit
    participant state."""
    session = _load_session(db, session_id)
    uid = _authorize(session, current_user)
    if uid != session.teacher_id and current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Only the teacher can mute a participant")
    if not lk_video.is_session_participant(db, session, target_user_id):
        raise HTTPException(status_code=404, detail="Not a participant of this session")

    room_key = lk_video.room_key_for_session(db, session)
    room_name = lk_video.room_name_for_session(room_key)
    muted = await lk_video.mute_participant_microphone(room_name, str(target_user_id))

    publish_classroom_event(room_key, {"type": "participant_muted", "user_id": str(target_user_id)})
    return {"muted": muted}


# ─── Recording (LiveKit Egress) ─────────────────────────────────────────────

class RecordingOut(BaseModel):
    id: str
    status: str  # 'active' | 'ending' | 'complete' | 'failed'
    file_url: Optional[str] = None
    duration_sec: Optional[int] = None
    started_at: str
    ended_at: Optional[str] = None


@router.post("/{session_id}/recording/start", response_model=RecordingOut, status_code=201)
async def start_recording(
    session_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Teacher (or admin) only — starts a room-composite recording. Only one
    active recording per session at a time (mirrors what the UI exposes)."""
    session = _load_session(db, session_id)
    uid = _authorize(session, current_user)
    if uid != session.teacher_id and current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Seul l'enseignant peut démarrer l'enregistrement")
    if not lk_egress.is_configured():
        raise HTTPException(status_code=503, detail="L'enregistrement n'est pas configuré sur cette plateforme (EGRESS_S3_*)")

    existing = db.exec(
        select(SessionRecording)
        .where(SessionRecording.session_id.in_(_group_session_ids(db, session)))
        .where(SessionRecording.status.in_(("active", "ending")))
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Un enregistrement est déjà en cours pour cette séance")

    room_key = lk_video.room_key_for_session(db, session)
    room_name = lk_video.room_name_for_session(room_key)
    try:
        info = await lk_egress.start_recording(room_name, str(session_id))
    except Exception:
        logger.exception("Failed to start egress recording session_id=%s", session_id)
        raise HTTPException(status_code=502, detail="Impossible de démarrer l'enregistrement")

    rec = SessionRecording(
        session_id=session_id, room_key=room_key, egress_id=info["egress_id"],
        status=info["status"], started_by=uid,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)

    publish_classroom_event(room_key, {"type": "recording_started"})
    return RecordingOut(id=str(rec.id), status=rec.status, started_at=rec.started_at.isoformat())


@router.post("/{session_id}/recording/{recording_id}/stop", response_model=RecordingOut)
async def stop_recording(
    session_id: UUID,
    recording_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = _load_session(db, session_id)
    uid = _authorize(session, current_user)
    if uid != session.teacher_id and current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Seul l'enseignant peut arrêter l'enregistrement")

    rec = db.get(SessionRecording, recording_id)
    if rec is None or rec.session_id not in _group_session_ids(db, session):
        raise HTTPException(status_code=404, detail="Recording not found")
    if rec.status not in ("active",):
        raise HTTPException(status_code=409, detail=f"Cannot stop a recording in status '{rec.status}'")

    try:
        info = await lk_egress.stop_recording(rec.egress_id)
    except Exception:
        logger.exception("Failed to stop egress recording id=%s", rec.egress_id)
        raise HTTPException(status_code=502, detail="Impossible d'arrêter l'enregistrement")

    rec.status = info["status"]
    rec.ended_at = datetime.now(timezone.utc)
    db.add(rec)
    db.commit()
    db.refresh(rec)

    publish_classroom_event(rec.room_key, {"type": "recording_stopped"})
    return RecordingOut(
        id=str(rec.id), status=rec.status, file_url=rec.file_url, duration_sec=rec.duration_sec,
        started_at=rec.started_at.isoformat(), ended_at=rec.ended_at.isoformat(),
    )


@router.get("/{session_id}/recordings", response_model=List[RecordingOut])
async def list_recordings(
    session_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Both participants can view — refreshes status/file_url from LiveKit's
    ListEgress for anything not yet 'complete'/'failed' (no webhook receiver
    exists in this app, see app/services/egress.py)."""
    session = _load_session(db, session_id)
    _authorize(session, current_user)

    recordings = db.exec(
        select(SessionRecording)
        .where(SessionRecording.session_id.in_(_group_session_ids(db, session)))
        .order_by(SessionRecording.started_at.desc())
    ).all()

    for rec in recordings:
        if rec.status in ("complete", "failed"):
            continue
        try:
            fresh = await lk_egress.get_egress_status(rec.egress_id)
        except Exception:
            logger.warning("Failed to refresh egress status id=%s", rec.egress_id, exc_info=True)
            continue
        if fresh is None:
            continue
        if fresh["status"] != rec.status or fresh.get("file_url"):
            rec.status = fresh["status"]
            if fresh.get("file_url"):
                rec.file_url = fresh["file_url"]
            if fresh.get("duration_sec"):
                rec.duration_sec = fresh["duration_sec"]
            if rec.status in ("complete", "failed") and rec.ended_at is None:
                rec.ended_at = datetime.now(timezone.utc)
            db.add(rec)
    db.commit()

    return [
        RecordingOut(
            id=str(r.id), status=r.status, file_url=r.file_url, duration_sec=r.duration_sec,
            started_at=r.started_at.isoformat(), ended_at=r.ended_at.isoformat() if r.ended_at else None,
        )
        for r in recordings
    ]


# ─── Shared notepad ─────────────────────────────────────────────────────────

class NotepadOut(BaseModel):
    content: str
    updated_by: Optional[str] = None
    updated_at: str


class NotepadIn(BaseModel):
    content: str


@router.get("/{session_id}/notepad", response_model=NotepadOut)
async def get_notepad(
    session_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = _load_session(db, session_id)
    _authorize(session, current_user)
    room_key = lk_video.room_key_for_session(db, session)
    pad = db.get(SessionNotepad, room_key)
    if pad is None:
        return NotepadOut(content="", updated_at=datetime.now(timezone.utc).isoformat())
    return NotepadOut(
        content=pad.content, updated_by=str(pad.updated_by) if pad.updated_by else None,
        updated_at=pad.updated_at.isoformat(),
    )


@router.put("/{session_id}/notepad", response_model=NotepadOut)
async def update_notepad(
    session_id: UUID,
    payload: NotepadIn,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Either party can edit — debounced client-side (not on every
    keystroke), broadcast to the room so it stays live without a poll."""
    session = _load_session(db, session_id)
    uid = _authorize(session, current_user)
    room_key = lk_video.room_key_for_session(db, session)

    pad = db.get(SessionNotepad, room_key)
    now = datetime.now(timezone.utc)
    if pad is None:
        pad = SessionNotepad(room_key=room_key, content=payload.content, updated_by=uid, updated_at=now)
    else:
        pad.content = payload.content
        pad.updated_by = uid
        pad.updated_at = now
    db.add(pad)
    db.commit()

    publish_classroom_event(room_key, {"type": "notepad_updated", "content": pad.content, "updated_by": str(uid)})
    return NotepadOut(content=pad.content, updated_by=str(uid), updated_at=pad.updated_at.isoformat())


# ─── Whiteboard background (shared exercise sheet / diagram image) ─────────

class WhiteboardBackgroundOut(BaseModel):
    background_url: Optional[str] = None


class WhiteboardBackgroundIn(BaseModel):
    file_id: UUID


@router.get("/{session_id}/whiteboard-background", response_model=WhiteboardBackgroundOut)
async def get_whiteboard_background(
    session_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = _load_session(db, session_id)
    _authorize(session, current_user)
    room_key = lk_video.room_key_for_session(db, session)
    state = db.get(SessionWhiteboardState, room_key)
    return WhiteboardBackgroundOut(background_url=state.background_url if state else None)


@router.put("/{session_id}/whiteboard-background", response_model=WhiteboardBackgroundOut)
async def set_whiteboard_background(
    session_id: UUID,
    payload: WhiteboardBackgroundIn,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Sets an already-uploaded image (see the Library file list) as the
    whiteboard's shared background — drawn behind replayed strokes on both
    sides, so annotating over an exercise sheet/diagram photo works the same
    way as the plain blank whiteboard."""
    session = _load_session(db, session_id)
    uid = _authorize(session, current_user)
    if uid != session.teacher_id and current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Seul l'enseignant peut définir le fond du tableau")
    room_key = lk_video.room_key_for_session(db, session)

    file = db.get(SessionFile, payload.file_id)
    if file is None or file.session_id not in _group_session_ids(db, session):
        raise HTTPException(status_code=404, detail="File not found")
    if not file.mime or not file.mime.startswith("image/"):
        raise HTTPException(status_code=400, detail="Seule une image peut être utilisée comme fond du tableau")

    state = db.get(SessionWhiteboardState, room_key)
    now = datetime.now(timezone.utc)
    if state is None:
        state = SessionWhiteboardState(room_key=room_key, background_url=file.url, background_file_id=file.id, updated_by=uid, updated_at=now)
    else:
        state.background_url = file.url
        state.background_file_id = file.id
        state.updated_by = uid
        state.updated_at = now
    db.add(state)
    db.commit()

    publish_classroom_event(room_key, {"type": "whiteboard_background_updated", "background_url": file.url})
    return WhiteboardBackgroundOut(background_url=file.url)


@router.delete("/{session_id}/whiteboard-background", status_code=204)
async def clear_whiteboard_background(
    session_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = _load_session(db, session_id)
    uid = _authorize(session, current_user)
    if uid != session.teacher_id and current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Seul l'enseignant peut retirer le fond du tableau")
    room_key = lk_video.room_key_for_session(db, session)

    state = db.get(SessionWhiteboardState, room_key)
    if state is not None:
        state.background_url = None
        state.background_file_id = None
        state.updated_by = uid
        state.updated_at = datetime.now(timezone.utc)
        db.add(state)
        db.commit()

    publish_classroom_event(room_key, {"type": "whiteboard_background_updated", "background_url": None})
    return None


class BookmarkIn(BaseModel):
    elapsed_sec: int
    label: Optional[str] = None


class BookmarkOut(BaseModel):
    id: str
    label: Optional[str] = None
    elapsed_sec: int
    created_by: str
    created_at: str


@router.post("/{session_id}/bookmarks", response_model=BookmarkOut, status_code=201)
async def create_bookmark(
    session_id: UUID,
    payload: BookmarkIn,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Either party can mark a moment — shown on the post-session summary
    page, linking into the recording once one exists (see
    app/routers/classroom.py's recording endpoints)."""
    session = _load_session(db, session_id)
    uid = _authorize(session, current_user)
    room_key = lk_video.room_key_for_session(db, session)

    bookmark = SessionBookmark(
        session_id=session_id, room_key=room_key, created_by=uid,
        label=payload.label, elapsed_sec=max(0, payload.elapsed_sec),
    )
    db.add(bookmark)
    db.commit()
    db.refresh(bookmark)
    return BookmarkOut(
        id=str(bookmark.id), label=bookmark.label, elapsed_sec=bookmark.elapsed_sec,
        created_by=str(bookmark.created_by), created_at=bookmark.created_at.isoformat(),
    )


@router.get("/{session_id}/bookmarks", response_model=List[BookmarkOut])
async def list_bookmarks(
    session_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = _load_session(db, session_id)
    _authorize(session, current_user)

    bookmarks = db.exec(
        select(SessionBookmark)
        .where(SessionBookmark.session_id.in_(_group_session_ids(db, session)))
        .order_by(SessionBookmark.elapsed_sec)
    ).all()
    return [
        BookmarkOut(
            id=str(b.id), label=b.label, elapsed_sec=b.elapsed_sec,
            created_by=str(b.created_by), created_at=b.created_at.isoformat(),
        )
        for b in bookmarks
    ]


class WhiteboardSnapshotOut(BaseModel):
    id: str
    image_url: str
    created_by: str
    created_at: str


@router.post("/{session_id}/whiteboard-snapshots", response_model=WhiteboardSnapshotOut, status_code=201)
async def create_whiteboard_snapshot(
    session_id: UUID,
    file: UploadFile = File(...),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Either party can export a PNG capture of the whiteboard (composited
    client-side from the background + drawn strokes) — shown on the
    post-session summary page next to recordings/bookmarks."""
    session = _load_session(db, session_id)
    uid = _authorize(session, current_user)

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Image vide")

    from app.services.storage import GENERAL_CONTENT_TYPES, GENERAL_EXTENSIONS, UploadValidationError, validate_upload
    try:
        validate_upload(
            filename=file.filename, content_type=file.content_type, size=len(raw),
            allowed_content_types=GENERAL_CONTENT_TYPES, allowed_extensions=GENERAL_EXTENSIONS,
            max_size=10 * 1024 * 1024,
        )
    except UploadValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    room_key = lk_video.room_key_for_session(db, session)
    image_url = upload_file(raw, file.filename or "whiteboard.png", file.content_type, folder=f"whiteboard-snapshots/{session_id}")

    snapshot = SessionWhiteboardSnapshot(session_id=session_id, room_key=room_key, created_by=uid, image_url=image_url)
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    return WhiteboardSnapshotOut(
        id=str(snapshot.id), image_url=snapshot.image_url,
        created_by=str(snapshot.created_by), created_at=snapshot.created_at.isoformat(),
    )


@router.get("/{session_id}/whiteboard-snapshots", response_model=List[WhiteboardSnapshotOut])
async def list_whiteboard_snapshots(
    session_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = _load_session(db, session_id)
    _authorize(session, current_user)

    snapshots = db.exec(
        select(SessionWhiteboardSnapshot)
        .where(SessionWhiteboardSnapshot.session_id.in_(_group_session_ids(db, session)))
        .order_by(SessionWhiteboardSnapshot.created_at)
    ).all()
    return [
        WhiteboardSnapshotOut(
            id=str(s.id), image_url=s.image_url,
            created_by=str(s.created_by), created_at=s.created_at.isoformat(),
        )
        for s in snapshots
    ]


@router.post("/{session_id}/dev-simulate-start")
async def dev_simulate_session_start(
    session_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    🧪 TEMPORARY TEST TOOL — NOT a real feature, remove after QA.

    Moves this session's `scheduled_at` to right now, so the REAL join-window
    gate in get_classroom_room() naturally opens — lets both the student and
    the teacher test the waiting-room → live-call flow without waiting for
    the actual scheduled time. This does not bypass any security/authorization
    check: the room/token endpoint still runs its normal participant check and
    its normal timing logic, just against a (deliberately) adjusted clock
    target. Either the session's student or its teacher can call this.
    """
    from app.config import get_settings as _get_settings
    if _get_settings().is_production:
        raise HTTPException(status_code=403, detail="Outil de test désactivé en production.")

    session = _load_session(db, session_id)
    _authorize(session, current_user)

    if session.status in ("cancelled", "no_show", "completed"):
        raise HTTPException(status_code=409, detail=f"Séance en statut '{session.status}' — impossible à simuler.")

    session.scheduled_at = datetime.now(timezone.utc)
    db.add(session)
    db.commit()
    db.refresh(session)

    logger.info("🧪 [TEST] Simulated session start: session_id=%s", session_id)
    return {"status": "ok", "scheduled_at": session.scheduled_at.isoformat()}


# ─── Group-lesson sibling sessions (read-only) ──────────────────────────────
# For a group lesson, every enrolled student has their OWN TutoringSession
# row (see module docstring), so the proof-of-attendance / trust-score flow
# (app/routers/session_validation.py) — which is keyed one-row-per-student —
# needs a way to go from ANY one of those sibling session ids to the full
# list, so the teacher can confirm each student's attendance individually
# right after a group lesson ends. Reuses the exact same group-resolution
# helpers as the video room endpoint above — no new logic invented here.

class GroupSessionSibling(BaseModel):
    session_id: str
    student_id: str
    student_name: str
    student_avatar: Optional[str] = None


class GroupSessionsResponse(BaseModel):
    session_type: str  # "individual" | "group"
    sessions: List[GroupSessionSibling]


@router.get("/{session_id}/group-sessions", response_model=GroupSessionsResponse)
async def get_group_sessions(
    session_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Given one TutoringSession id, resolve the sibling session ids for the
    same group lesson (one per enrolled student). For an individual session,
    returns just that one session. Teacher (or admin) only."""
    session = _load_session(db, session_id)
    uid = _authorize(session, current_user)
    if uid != session.teacher_id and current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Réservé à l'enseignant de la séance")

    slot_id = _group_slot_id(db, session)
    if not slot_id:
        student = db.exec(select(Profile).where(Profile.id == session.student_id)).first()
        return GroupSessionsResponse(session_type="individual", sessions=[
            GroupSessionSibling(
                session_id=str(session.id),
                student_id=str(session.student_id),
                student_name=_display_name(student),
                student_avatar=student.avatar_url if student else None,
            ),
        ])

    sibling_sessions = _group_sessions(db, slot_id)
    students = db.exec(
        select(Profile).where(Profile.id.in_({s.student_id for s in sibling_sessions}))
    ).all()
    profile_map = {p.id: p for p in students}
    siblings = sorted(
        (
            GroupSessionSibling(
                session_id=str(s.id),
                student_id=str(s.student_id),
                student_name=_display_name(profile_map.get(s.student_id)),
                student_avatar=(profile_map.get(s.student_id).avatar_url if profile_map.get(s.student_id) else None),
            )
            for s in sibling_sessions
        ),
        key=lambda item: item.student_name,
    )
    return GroupSessionsResponse(session_type="group", sessions=siblings)


# ─── In-call chat provisioning ──────────────────────────────────────────────

@router.get("/{session_id}/chat")
async def get_classroom_chat(
    session_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Find-or-create the real conversation for this lesson's participants —
    messages sent here are the SAME persisted messages visible afterward in
    the normal inbox (see app/routers/messages.py), not a throwaway
    in-call-only chat."""
    session = _load_session(db, session_id)
    uid = _authorize(session, current_user)

    slot_id = _group_slot_id(db, session)
    if slot_id:
        member_ids = sorted({str(session.teacher_id)} | {str(s.student_id) for s in _group_sessions(db, slot_id)})
        conv_type = "group"
    else:
        member_ids = sorted({str(session.teacher_id), str(session.student_id)})
        conv_type = "direct"

    # Find an existing conversation whose participant set matches exactly.
    candidate_convs: Dict[UUID, set] = {}
    rows = db.exec(
        select(ConversationParticipant).where(ConversationParticipant.user_id == uid)
    ).all()
    for r in rows:
        members = {
            str(p.user_id) for p in db.exec(
                select(ConversationParticipant).where(ConversationParticipant.conv_id == r.conv_id)
            ).all()
        }
        if members == set(member_ids):
            conv = db.get(Conversation, r.conv_id)
            if conv and conv.type == conv_type:
                # Unhide for me if I'd previously hidden it
                my_part = db.exec(
                    select(ConversationParticipant)
                    .where(ConversationParticipant.conv_id == r.conv_id, ConversationParticipant.user_id == uid)
                ).first()
                if my_part and my_part.hidden_at is not None:
                    my_part.hidden_at = None
                    db.add(my_part)
                    db.commit()
                return {"conversation_id": str(r.conv_id)}

    conv = Conversation(id=uuid4(), type=conv_type)
    db.add(conv)
    db.flush()
    for member_id in member_ids:
        db.add(ConversationParticipant(conv_id=conv.id, user_id=UUID(member_id), role="member"))
    db.commit()
    return {"conversation_id": str(conv.id)}


# ─── Chapters / agenda ───────────────────────────────────────────────────────

class ChapterIn(BaseModel):
    title: str
    duration_min: int = 10


class ChapterOut(BaseModel):
    id: str
    title: str
    duration_min: int
    position: int
    status: str
    started_at: Optional[str] = None


def _chapter_out(c: SessionChapter) -> ChapterOut:
    return ChapterOut(
        id=str(c.id), title=c.title, duration_min=c.duration_min, position=c.position,
        status=c.status, started_at=c.started_at.isoformat() if c.started_at else None,
    )


@router.get("/{session_id}/chapters", response_model=List[ChapterOut])
async def list_chapters(
    session_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = _load_session(db, session_id)
    _authorize(session, current_user)
    chapters = db.exec(
        select(SessionChapter).where(SessionChapter.session_id == session_id).order_by(SessionChapter.position)
    ).all()
    return [_chapter_out(c) for c in chapters]


@router.post("/{session_id}/chapters", response_model=ChapterOut, status_code=201)
async def create_chapter(
    session_id: UUID,
    payload: ChapterIn,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = _load_session(db, session_id)
    uid = _authorize(session, current_user)
    if uid != session.teacher_id and current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Seul l'enseignant peut gérer le programme de la séance")

    count = len(db.exec(select(SessionChapter).where(SessionChapter.session_id == session_id)).all())
    chapter = SessionChapter(session_id=session_id, title=payload.title, duration_min=payload.duration_min, position=count)
    db.add(chapter)
    db.commit()
    db.refresh(chapter)
    out = _chapter_out(chapter)
    publish_classroom_event(lk_video.room_key_for_session(db, session), {"type": "chapter_created", "chapter": out.model_dump()})
    return out


@router.delete("/{session_id}/chapters/{chapter_id}", status_code=204)
async def delete_chapter(
    session_id: UUID,
    chapter_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = _load_session(db, session_id)
    uid = _authorize(session, current_user)
    if uid != session.teacher_id and current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Seul l'enseignant peut gérer le programme de la séance")
    chapter = db.get(SessionChapter, chapter_id)
    if chapter is None or chapter.session_id != session_id:
        raise HTTPException(status_code=404, detail="Chapter not found")
    db.delete(chapter)
    db.commit()
    publish_classroom_event(lk_video.room_key_for_session(db, session), {"type": "chapter_deleted", "chapter_id": str(chapter_id)})
    return None


@router.post("/{session_id}/chapters/{chapter_id}/advance", response_model=ChapterOut)
async def advance_chapter(
    session_id: UUID,
    chapter_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = _load_session(db, session_id)
    uid = _authorize(session, current_user)
    if uid != session.teacher_id and current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Seul l'enseignant peut avancer le programme de la séance")

    target = db.get(SessionChapter, chapter_id)
    if target is None or target.session_id != session_id:
        raise HTTPException(status_code=404, detail="Chapter not found")

    all_chapters = db.exec(select(SessionChapter).where(SessionChapter.session_id == session_id)).all()
    now = datetime.now(timezone.utc)
    for c in all_chapters:
        if c.id == target.id:
            c.status = "active"
            c.started_at = now
        elif c.status == "active":
            c.status = "done"
        db.add(c)
    db.commit()
    db.refresh(target)
    out = _chapter_out(target)
    publish_classroom_event(lk_video.room_key_for_session(db, session), {"type": "chapter_advanced", "chapter": out.model_dump()})
    return out


# ─── Files ──────────────────────────────────────────────────────────────────

H5P_MAX_PACKAGE_SIZE = 100 * 1024 * 1024  # 100 MB, uncompressed


def _extract_and_host_h5p(raw: bytes, session_id: UUID) -> str:
    """Extracts a `.h5p` package (a zip archive) and re-uploads every entry to
    Supabase Storage under a fresh folder, preserving relative paths — the
    h5p-standalone player fetches h5p.json/content.json/library files by
    plain relative URL against this folder's base, so it can't work from a
    single opaque .h5p file URL the way a PDF/image can.

    Returns the base folder's public URL (used directly as h5pJsonPath).
    """
    from app.services.storage import upload_at_path, public_folder_url

    folder = f"h5p-content/{session_id}/{uuid4()}"
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Fichier H5P invalide (zip corrompu)")

    total_size = 0
    entries: list[tuple[str, zipfile.ZipInfo]] = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        # Reject zip-slip attempts (entries that normalize outside the
        # archive root, e.g. "../../etc/passwd") — Supabase Storage doesn't
        # do real filesystem traversal, but there's no reason to trust these
        # paths any further than necessary.
        normalized = posixpath.normpath(info.filename)
        if normalized.startswith("..") or normalized.startswith("/"):
            continue
        total_size += info.file_size
        if total_size > H5P_MAX_PACKAGE_SIZE:
            raise HTTPException(status_code=400, detail="Paquet H5P trop volumineux")
        entries.append((normalized, info))

    if not any(name == "h5p.json" for name, _ in entries):
        raise HTTPException(status_code=400, detail="Paquet H5P invalide (h5p.json manquant)")

    for normalized, info in entries:
        upload_at_path(zf.read(info), f"{folder}/{normalized}")

    return public_folder_url(folder)


class FileOut(BaseModel):
    id: str
    name: str
    url: str
    mime: Optional[str] = None
    size_bytes: Optional[int] = None
    uploaded_by: str
    created_at: str


def _file_out(f: SessionFile) -> FileOut:
    return FileOut(
        id=str(f.id), name=f.name, url=f.url, mime=f.mime, size_bytes=f.size_bytes,
        uploaded_by=str(f.uploaded_by), created_at=f.created_at.isoformat(),
    )


@router.get("/{session_id}/files", response_model=List[FileOut])
async def list_session_files(
    session_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = _load_session(db, session_id)
    _authorize(session, current_user)
    files = db.exec(
        select(SessionFile).where(SessionFile.session_id == session_id).order_by(SessionFile.created_at.desc())
    ).all()
    return [_file_out(f) for f in files]


@router.post("/{session_id}/files", response_model=FileOut, status_code=201)
async def upload_session_file(
    session_id: UUID,
    file: UploadFile = File(...),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = _load_session(db, session_id)
    uid = _authorize(session, current_user)
    if uid != session.teacher_id and current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Seul l'enseignant peut ajouter des fichiers à la séance")

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Fichier vide")

    is_h5p = (file.filename or "").lower().endswith(".h5p")
    if is_h5p:
        if len(raw) > H5P_MAX_PACKAGE_SIZE:
            raise HTTPException(status_code=400, detail="Paquet H5P trop volumineux")
        url = _extract_and_host_h5p(raw, session_id)
    else:
        from app.services.storage import (
            GENERAL_CONTENT_TYPES, GENERAL_EXTENSIONS, UploadValidationError, validate_upload,
        )
        try:
            validate_upload(
                filename=file.filename, content_type=file.content_type, size=len(raw),
                allowed_content_types=GENERAL_CONTENT_TYPES, allowed_extensions=GENERAL_EXTENSIONS,
                max_size=50 * 1024 * 1024,
            )
        except UploadValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        url = upload_file(raw, file.filename or "fichier", file.content_type, folder=f"session-files/{session_id}")

    record = SessionFile(
        session_id=session_id, uploaded_by=uid, name=file.filename or "fichier",
        url=url, mime=file.content_type, size_bytes=len(raw),
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    out = _file_out(record)
    publish_classroom_event(lk_video.room_key_for_session(db, session), {"type": "file_added", "file": out.model_dump()})
    return out


@router.delete("/{session_id}/files/{file_id}", status_code=204)
async def delete_session_file(
    session_id: UUID,
    file_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = _load_session(db, session_id)
    uid = _authorize(session, current_user)
    if uid != session.teacher_id and current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Seul l'enseignant peut retirer un fichier")
    record = db.get(SessionFile, file_id)
    if record is None or record.session_id != session_id:
        raise HTTPException(status_code=404, detail="File not found")
    db.delete(record)
    db.commit()
    publish_classroom_event(lk_video.room_key_for_session(db, session), {"type": "file_removed", "file_id": str(file_id)})
    return None


# ─── Live quizzes ────────────────────────────────────────────────────────────

class QuizIn(BaseModel):
    question: str
    choices: List[str]
    correct_indices: List[int]


class QuizAnswerIn(BaseModel):
    choice_indices: List[int]


class QuizOut(BaseModel):
    id: str
    question: str
    choices: List[str]
    created_at: str
    my_answers: Optional[List[int]] = None
    my_correct: Optional[bool] = None
    correct_indices: Optional[List[int]] = None  # only revealed to the teacher, or after answering
    answers_count: Optional[int] = None
    correct_count: Optional[int] = None


@router.post("/{session_id}/quizzes", response_model=QuizOut, status_code=201)
async def create_quiz(
    session_id: UUID,
    payload: QuizIn,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = _load_session(db, session_id)
    uid = _authorize(session, current_user)
    if uid != session.teacher_id and current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Seul l'enseignant peut envoyer un quiz")
    if len(payload.choices) < 2:
        raise HTTPException(status_code=400, detail="Quiz invalide")
    if not payload.correct_indices or any(not (0 <= i < len(payload.choices)) for i in payload.correct_indices):
        raise HTTPException(status_code=400, detail="Quiz invalide")

    correct_sorted = sorted(set(payload.correct_indices))
    quiz = SessionQuiz(
        session_id=session_id, created_by=uid, question=payload.question,
        choices=payload.choices, correct_index=correct_sorted[0], correct_indices=correct_sorted,
    )
    db.add(quiz)
    db.commit()
    db.refresh(quiz)

    # Broadcast a STUDENT-SAFE payload — never leak correct_indices to the room.
    publish_classroom_event(lk_video.room_key_for_session(db, session), {
        "type": "quiz_dispatched",
        "quiz": {"id": str(quiz.id), "question": quiz.question, "choices": quiz.choices,
                 "created_at": quiz.created_at.isoformat()},
    })
    return QuizOut(
        id=str(quiz.id), question=quiz.question, choices=quiz.choices,
        created_at=quiz.created_at.isoformat(), correct_indices=quiz.correct_indices,
        answers_count=0, correct_count=0,
    )


@router.get("/{session_id}/quizzes", response_model=List[QuizOut])
async def list_quizzes(
    session_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = _load_session(db, session_id)
    uid = _authorize(session, current_user)
    is_teacher = uid == session.teacher_id or current_user.get("role") == "admin"

    quizzes = db.exec(
        select(SessionQuiz)
        .where(SessionQuiz.session_id.in_(_group_session_ids(db, session)))
        .order_by(SessionQuiz.created_at)
    ).all()
    quiz_ids = [q.id for q in quizzes]
    answers_by_quiz: Dict[UUID, List[SessionQuizAnswer]] = {qid: [] for qid in quiz_ids}
    if quiz_ids:
        for a in db.exec(select(SessionQuizAnswer).where(SessionQuizAnswer.quiz_id.in_(quiz_ids))).all():
            answers_by_quiz.setdefault(a.quiz_id, []).append(a)

    out: List[QuizOut] = []
    for q in quizzes:
        all_answers = answers_by_quiz.get(q.id, [])
        my_answer = next((a for a in all_answers if a.student_id == uid), None)
        out.append(QuizOut(
            id=str(q.id), question=q.question, choices=q.choices, created_at=q.created_at.isoformat(),
            my_answers=my_answer.choice_indices if my_answer else None,
            my_correct=my_answer.is_correct if my_answer else None,
            correct_indices=q.correct_indices if (is_teacher or my_answer) else None,
            answers_count=len(all_answers) if is_teacher else None,
            correct_count=sum(1 for a in all_answers if a.is_correct) if is_teacher else None,
        ))
    return out


@router.post("/{session_id}/quizzes/{quiz_id}/answer", response_model=QuizOut)
async def answer_quiz(
    session_id: UUID,
    quiz_id: UUID,
    payload: QuizAnswerIn,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = _load_session(db, session_id)
    uid = _authorize(session, current_user)
    if uid == session.teacher_id:
        raise HTTPException(status_code=403, detail="L'enseignant ne répond pas à son propre quiz")

    quiz = db.get(SessionQuiz, quiz_id)
    if quiz is None or quiz.session_id not in _group_session_ids(db, session):
        raise HTTPException(status_code=404, detail="Quiz not found")
    existing = db.exec(
        select(SessionQuizAnswer).where(SessionQuizAnswer.quiz_id == quiz_id, SessionQuizAnswer.student_id == uid)
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Vous avez déjà répondu à ce quiz")
    choice_indices = sorted(set(payload.choice_indices))
    if not choice_indices or any(not (0 <= i < len(quiz.choices)) for i in choice_indices):
        raise HTTPException(status_code=400, detail="Réponse invalide")

    # Correct only if the student picked exactly the set of correct choices —
    # no partial credit for a multi-answer question (picking 1 of 2 correct
    # choices, or picking a correct one plus a wrong one, doesn't count).
    is_correct = choice_indices == sorted(quiz.correct_indices)
    answer = SessionQuizAnswer(
        quiz_id=quiz_id, student_id=uid, choice_index=choice_indices[0],
        choice_indices=choice_indices, is_correct=is_correct,
    )
    db.add(answer)
    db.commit()

    if is_correct:
        try:
            award_kp(uid, QUIZ_CORRECT_ANSWER_EP, KpSource.quiz, "Bonne réponse — quiz en séance", db)
        except Exception:
            logger.exception("award_kp failed for quiz answer uid=%s quiz=%s", uid, quiz_id)

    all_answers = db.exec(select(SessionQuizAnswer).where(SessionQuizAnswer.quiz_id == quiz_id)).all()
    answers_count = len(all_answers)
    correct_count = sum(1 for a in all_answers if a.is_correct)

    # Teacher-only tally update (never broadcasts correct_indices to students).
    publish_classroom_event(lk_video.room_key_for_session(db, session), {
        "type": "quiz_tally_update",
        "quiz_id": str(quiz_id), "answers_count": answers_count, "correct_count": correct_count,
    })

    return QuizOut(
        id=str(quiz.id), question=quiz.question, choices=quiz.choices, created_at=quiz.created_at.isoformat(),
        my_answers=choice_indices, my_correct=is_correct, correct_indices=quiz.correct_indices,
        answers_count=answers_count, correct_count=correct_count,
    )


class QuizStudentAnswer(BaseModel):
    student_id: str
    student_name: str
    student_avatar: Optional[str] = None
    choice_indices: Optional[List[int]] = None  # None = hasn't answered yet
    is_correct: Optional[bool] = None
    answered_at: Optional[str] = None


class QuizAnswersDetail(BaseModel):
    quiz_id: str
    question: str
    choices: List[str]
    correct_indices: List[int]
    students: List[QuizStudentAnswer]  # every enrolled student, answered or not


@router.get("/{session_id}/quizzes/{quiz_id}/answers", response_model=QuizAnswersDetail)
async def get_quiz_answers(
    session_id: UUID,
    quiz_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Per-student answer breakdown for one quiz — teacher (or admin) only.
    Lists every enrolled student (individual: the one student; group: every
    sibling session's student, see _group_session_ids), so the teacher can
    tell "hasn't answered yet" apart from "answered incorrectly"."""
    session = _load_session(db, session_id)
    uid = _authorize(session, current_user)
    if uid != session.teacher_id and current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Seul l'enseignant peut voir le détail des réponses")

    quiz = db.get(SessionQuiz, quiz_id)
    if quiz is None or quiz.session_id not in _group_session_ids(db, session):
        raise HTTPException(status_code=404, detail="Quiz not found")

    slot_id = _group_slot_id(db, session)
    if slot_id:
        student_ids = list({s.student_id for s in _group_sessions(db, slot_id)})
    else:
        student_ids = [session.student_id]

    students = db.exec(select(Profile).where(Profile.id.in_(student_ids))).all()
    profile_map = {p.id: p for p in students}
    answers = db.exec(select(SessionQuizAnswer).where(SessionQuizAnswer.quiz_id == quiz_id)).all()
    answer_map = {a.student_id: a for a in answers}

    out_students = sorted(
        (
            QuizStudentAnswer(
                student_id=str(sid),
                student_name=_display_name(profile_map.get(sid)),
                student_avatar=(profile_map.get(sid).avatar_url if profile_map.get(sid) else None),
                choice_indices=(answer_map[sid].choice_indices if sid in answer_map else None),
                is_correct=(answer_map[sid].is_correct if sid in answer_map else None),
                answered_at=(answer_map[sid].answered_at.isoformat() if sid in answer_map else None),
            )
            for sid in student_ids
        ),
        key=lambda s: (s.choice_indices is None, s.student_name),
    )
    return QuizAnswersDetail(
        quiz_id=str(quiz.id), question=quiz.question, choices=quiz.choices,
        correct_indices=quiz.correct_indices, students=out_students,
    )
