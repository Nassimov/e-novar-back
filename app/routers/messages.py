"""
Chat router — conversations, messages, file uploads.

Permission model (updated):
  student  → teacher   allowed for any approved teacher (no booking required)
  teacher  → student   requires a Booking or TutoringSession (teacher cannot cold-contact)
  parent   ↔ teacher   requires parent_student_link + booking/session on the child
  teacher  → parent    symmetric of the above
"""
from __future__ import annotations

import html
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.conversation import ChatMessage, Conversation, ConversationParticipant
from app.models.profile import Profile

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Messages"])

_MAX_BODY = 4000
_MAX_ATTACH = 5
_MAX_FILE = 10 * 1024 * 1024
_RATE_WINDOW = 60
_RATE_MAX = 30


# ── Suspicious-content scanner ────────────────────────────────────────────────

_SUSPICIOUS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b0[567]\d{8}\b", re.I), "numéro de téléphone"),
    (re.compile(r"\+213\s*\d{8,9}", re.I), "numéro international"),
    (re.compile(r"\bwhatsapp\b", re.I), "WhatsApp"),
    (re.compile(r"\btelegram\b", re.I), "Telegram"),
    (re.compile(r"\bviber\b", re.I), "Viber"),
    (re.compile(r"\bbaridimob\b", re.I), "BaridiMob"),
    (re.compile(r"\bccp\s*\d", re.I), "paiement CCP"),
    (re.compile(r"\bwestern\s+union\b", re.I), "Western Union"),
    (re.compile(r"\bmoneygram\b", re.I), "MoneyGram"),
    (re.compile(r"hors\s+(?:de\s+)?(?:la\s+)?plateforme", re.I), "hors plateforme"),
    (re.compile(r"en\s+dehors\s+de", re.I), "hors plateforme"),
    (re.compile(r"payer?\s+directement", re.I), "paiement direct"),
    (re.compile(r"virement\s+(?:direct|priv)", re.I), "virement direct"),
    (re.compile(r"\binstagram\b", re.I), "Instagram"),
    (re.compile(r"\bfacebook\b|\bfb\.com\b|\bfb\.me\b", re.I), "Facebook"),
    (re.compile(r"\bsnapchat\b", re.I), "Snapchat"),
]


def _scan_suspicious(body: str) -> Optional[str]:
    if not body:
        return None
    for pattern, reason in _SUSPICIOUS:
        if pattern.search(body):
            return reason
    return None


# ── Pydantic I/O ──────────────────────────────────────────────────────────────

class AttachmentIn(BaseModel):
    name: str
    url: str
    type: str
    size: int


class AttachmentOut(BaseModel):
    name: str
    url: str
    type: str
    size: int


class MessageOut(BaseModel):
    id: str
    conv_id: str
    sender_id: str
    body: Optional[str]
    attachments: List[AttachmentOut]
    created_at: str
    read_at: Optional[str]
    is_mine: bool
    reply_to_id: Optional[str] = None
    replied_body: Optional[str] = None
    replied_sender_name: Optional[str] = None


class ConversationOut(BaseModel):
    id: str
    other_user_id: str
    other_name: str
    other_role: str
    other_avatar: Optional[str]
    last_message: Optional[str]
    last_message_at: Optional[str]
    unread_count: int
    is_online: bool
    last_seen_at: Optional[str]
    other_last_read_at: Optional[str] = None


class SendMessageIn(BaseModel):
    body: Optional[str] = None
    attachments: Optional[List[AttachmentIn]] = None
    reply_to_id: Optional[str] = None


class CreateConvIn(BaseModel):
    recipient_id: str


# ── Internal helpers ──────────────────────────────────────────────────────────

def _me(current_user: Dict[str, Any]) -> UUID:
    return UUID(current_user["id"])


def _is_online(user_id: str) -> bool:
    try:
        from app.core.connections import chat_connections
        if chat_connections.is_connected(user_id):
            return True
    except Exception:
        pass
    try:
        from app.core.redis import get_redis_client
        return bool(get_redis_client().sismember("chat:online_users", user_id))
    except Exception:
        return False


def _rate_limit(user_id: str) -> None:
    from app.core.rate_limiter import check_rate_limit
    check_rate_limit(
        key=f"chat:rate:{user_id}", limit=_RATE_MAX, window_seconds=_RATE_WINDOW,
        message="Trop de messages. Réessayez dans 1 minute.",
    )


def _publish(channel: str, event: dict) -> None:
    try:
        from app.core.redis import get_redis_client
        get_redis_client().publish(channel, json.dumps(event))
    except Exception as exc:
        logger.debug("Redis publish skipped: %s", exc)


def _participant_ids(conv_id: UUID, db: Session) -> List[str]:
    return [
        str(p.user_id)
        for p in db.exec(
            select(ConversationParticipant).where(ConversationParticipant.conv_id == conv_id)
        ).all()
    ]


def _assert_participant(me: UUID, conv_id: UUID, db: Session) -> None:
    ok = db.exec(
        select(ConversationParticipant).where(
            ConversationParticipant.conv_id == conv_id,
            ConversationParticipant.user_id == me,
        )
    ).first()
    if not ok:
        raise HTTPException(status_code=403, detail="Accès refusé.")


def _get_profile(uid: UUID, db: Session) -> Optional[Profile]:
    return db.exec(select(Profile).where(Profile.id == uid)).first()


def _get_role(uid: UUID, db: Session) -> str:
    from app.models.profile import UserRole
    row = db.exec(select(UserRole).where(UserRole.user_id == uid)).first()
    return row.role if row else "student"


def _can_chat(me: UUID, me_role: str, them: UUID, them_role: str, db: Session) -> bool:
    """
    Return True if the two users are allowed to start a NEW conversation.
    Rules:
      student  → teacher:  always allowed if teacher status = 'approved'
      teacher  → student:  requires an existing Booking or TutoringSession
      parent   → teacher:  requires parent_student_link + booking on the child
      teacher  → parent:   symmetric of parent→teacher
    """
    from app.models.booking import Booking, TutoringSession

    def _has_booking(student: UUID, teacher: UUID) -> bool:
        return bool(
            db.exec(
                select(Booking).where(
                    Booking.student_id == student, Booking.teacher_id == teacher
                )
            ).first()
            or db.exec(
                select(TutoringSession).where(
                    TutoringSession.student_id == student,
                    TutoringSession.teacher_id == teacher,
                )
            ).first()
        )

    if me_role == "student" and them_role == "teacher":
        from app.models.profile import TeacherProfile
        tp = db.exec(select(TeacherProfile).where(TeacherProfile.user_id == them)).first()
        return tp is not None and tp.status == "approved"

    if me_role == "teacher" and them_role == "student":
        return _has_booking(them, me)

    if me_role in ("student", "teacher") and them_role in ("student", "teacher"):
        return False

    def _parent_teacher(parent: UUID, teacher: UUID) -> bool:
        from app.models.parent_link import ParentStudentLink
        links = db.exec(
            select(ParentStudentLink).where(ParentStudentLink.parent_id == parent)
        ).all()
        return any(_has_booking(lnk.student_id, teacher) for lnk in links)

    if me_role == "parent" and them_role == "teacher":
        return _parent_teacher(me, them)

    if me_role == "teacher" and them_role == "parent":
        return _parent_teacher(them, me)

    return False


def _fmt(
    msg: ChatMessage,
    me: UUID,
    replied_body: Optional[str] = None,
    replied_sender_name: Optional[str] = None,
) -> MessageOut:
    atts: List[AttachmentOut] = []
    if msg.attachments and isinstance(msg.attachments, list):
        atts = [
            AttachmentOut(
                name=a.get("name", ""),
                url=a.get("url", ""),
                type=a.get("type", ""),
                size=a.get("size", 0),
            )
            for a in msg.attachments
        ]
    return MessageOut(
        id=str(msg.id),
        conv_id=str(msg.conv_id),
        sender_id=str(msg.sender_id),
        body=msg.body,
        attachments=atts,
        created_at=msg.created_at.isoformat(),
        read_at=msg.read_at.isoformat() if msg.read_at else None,
        is_mine=msg.sender_id == me,
        reply_to_id=str(msg.reply_to_id) if msg.reply_to_id else None,
        replied_body=replied_body,
        replied_sender_name=replied_sender_name,
    )


# ── REST endpoints ────────────────────────────────────────────────────────────

@router.get("/unread-count")
def get_unread_message_count(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Total unread message count across every conversation — feeds the
    sidebar 'Messages' badge (see src/lib/badge-store.ts)."""
    from sqlalchemy import func

    me = _me(current_user)
    my_parts = db.exec(
        select(ConversationParticipant).where(
            ConversationParticipant.user_id == me,
            ConversationParticipant.hidden_at.is_(None),
        )
    ).all()

    total = 0
    for part in my_parts:
        stmt = select(func.count()).select_from(ChatMessage).where(
            ChatMessage.conv_id == part.conv_id,
            ChatMessage.sender_id != me,
        )
        if part.last_read_at:
            stmt = stmt.where(ChatMessage.created_at > part.last_read_at)
        total += db.exec(stmt).one()

    return {"unread_count": total}


@router.get("/", response_model=List[ConversationOut])
def list_conversations(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    me = _me(current_user)
    my_parts = db.exec(
        select(ConversationParticipant).where(
            ConversationParticipant.user_id == me,
            ConversationParticipant.hidden_at.is_(None),
        )
    ).all()
    if not my_parts:
        return []

    result: List[ConversationOut] = []
    for part in my_parts:
        cid = part.conv_id
        other_part = db.exec(
            select(ConversationParticipant).where(
                ConversationParticipant.conv_id == cid,
                ConversationParticipant.user_id != me,
            )
        ).first()
        if not other_part:
            continue

        other = _get_profile(other_part.user_id, db)
        if not other:
            continue

        last_msg = db.exec(
            select(ChatMessage)
            .where(ChatMessage.conv_id == cid)
            .order_by(ChatMessage.created_at.desc())
        ).first()

        stmt = select(ChatMessage).where(
            ChatMessage.conv_id == cid,
            ChatMessage.sender_id != me,
        )
        if part.last_read_at:
            stmt = stmt.where(ChatMessage.created_at > part.last_read_at)
        unread_count = len(db.exec(stmt).all())

        result.append(ConversationOut(
            id=str(cid),
            other_user_id=str(other_part.user_id),
            other_name=other.full_name or "Utilisateur",
            other_role=_get_role(other_part.user_id, db),
            other_avatar=other.avatar_url,
            last_message=last_msg.body if last_msg else None,
            last_message_at=last_msg.created_at.isoformat() if last_msg else None,
            unread_count=unread_count,
            is_online=_is_online(str(other_part.user_id)),
            last_seen_at=other.last_seen_at.isoformat() if other.last_seen_at else None,
            other_last_read_at=other_part.last_read_at.isoformat() if other_part.last_read_at else None,
        ))

    result.sort(key=lambda c: c.last_message_at or "", reverse=True)
    return result


@router.post("/", response_model=ConversationOut, status_code=status.HTTP_201_CREATED)
def create_or_get_conversation(
    payload: CreateConvIn,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    me = _me(current_user)
    them = UUID(payload.recipient_id)

    if me == them:
        raise HTTPException(status_code=400, detail="Vous ne pouvez pas vous écrire à vous-même.")

    them_profile = _get_profile(them, db)
    if not them_profile:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable.")

    me_role = current_user.get("role", "student")
    them_role = _get_role(them, db)

    if not _can_chat(me, me_role, them, them_role, db):
        raise HTTPException(
            status_code=403,
            detail="Ce profil enseignant n'est pas disponible pour la messagerie.",
        )

    my_conv_ids = {
        p.conv_id
        for p in db.exec(
            select(ConversationParticipant).where(ConversationParticipant.user_id == me)
        ).all()
    }
    existing_conv_id: Optional[UUID] = None
    for cid in my_conv_ids:
        match = db.exec(
            select(ConversationParticipant).where(
                ConversationParticipant.conv_id == cid,
                ConversationParticipant.user_id == them,
            )
        ).first()
        if match:
            existing_conv_id = cid
            break

    if existing_conv_id:
        my_part = db.exec(
            select(ConversationParticipant).where(
                ConversationParticipant.conv_id == existing_conv_id,
                ConversationParticipant.user_id == me,
            )
        ).first()
        if my_part and my_part.hidden_at is not None:
            my_part.hidden_at = None
            db.add(my_part)
            db.commit()
        conv_id = existing_conv_id
    else:
        conv = Conversation(id=uuid4(), type="direct", created_at=datetime.now(timezone.utc))
        db.add(conv)
        db.flush()
        db.add(ConversationParticipant(conv_id=conv.id, user_id=me, role="member"))
        db.add(ConversationParticipant(conv_id=conv.id, user_id=them, role="member"))
        db.commit()
        conv_id = conv.id

    last_msg = db.exec(
        select(ChatMessage)
        .where(ChatMessage.conv_id == conv_id)
        .order_by(ChatMessage.created_at.desc())
    ).first()

    other_part_obj = db.exec(
        select(ConversationParticipant).where(
            ConversationParticipant.conv_id == conv_id,
            ConversationParticipant.user_id == them,
        )
    ).first()

    return ConversationOut(
        id=str(conv_id),
        other_user_id=str(them),
        other_name=them_profile.full_name or "Utilisateur",
        other_role=them_role,
        other_avatar=them_profile.avatar_url,
        last_message=last_msg.body if last_msg else None,
        last_message_at=last_msg.created_at.isoformat() if last_msg else None,
        unread_count=0,
        is_online=_is_online(str(them)),
        last_seen_at=them_profile.last_seen_at.isoformat() if them_profile.last_seen_at else None,
        other_last_read_at=other_part_obj.last_read_at.isoformat() if other_part_obj and other_part_obj.last_read_at else None,
    )


@router.get("/{conv_id}/messages", response_model=List[MessageOut])
def get_messages(
    conv_id: UUID,
    before: Optional[str] = Query(None),
    size: int = Query(50, ge=1, le=100),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    me = _me(current_user)
    _assert_participant(me, conv_id, db)

    stmt = select(ChatMessage).where(ChatMessage.conv_id == conv_id)
    if before:
        try:
            before_dt = datetime.fromisoformat(before)
            stmt = stmt.where(ChatMessage.created_at < before_dt)
        except ValueError:
            pass
    stmt = stmt.order_by(ChatMessage.created_at.desc()).limit(size)

    msgs = list(reversed(db.exec(stmt).all()))

    # Batch-load reply context
    reply_ids = {m.reply_to_id for m in msgs if m.reply_to_id}
    replied_map: dict[UUID, ChatMessage] = {}
    if reply_ids:
        replied_msgs = db.exec(select(ChatMessage).where(ChatMessage.id.in_(reply_ids))).all()
        replied_map = {rm.id: rm for rm in replied_msgs}

    sender_ids = {rm.sender_id for rm in replied_map.values()}
    sender_names: dict[UUID, str] = {}
    if sender_ids:
        profiles = db.exec(select(Profile).where(Profile.id.in_(sender_ids))).all()
        sender_names = {p.id: (p.full_name or "Utilisateur") for p in profiles}

    result = []
    for m in msgs:
        rb: Optional[str] = None
        rsn: Optional[str] = None
        if m.reply_to_id and m.reply_to_id in replied_map:
            rm = replied_map[m.reply_to_id]
            rb = rm.body
            rsn = sender_names.get(rm.sender_id, "Utilisateur")
        result.append(_fmt(m, me, rb, rsn))
    return result


@router.post("/{conv_id}/messages", response_model=MessageOut, status_code=status.HTTP_201_CREATED)
async def send_message(
    conv_id: UUID,
    payload: SendMessageIn,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    me = _me(current_user)
    _assert_participant(me, conv_id, db)
    _rate_limit(str(me))

    body = html.escape(payload.body[:_MAX_BODY]) if payload.body else None
    atts = payload.attachments or []
    if not body and not atts:
        raise HTTPException(status_code=400, detail="Message vide.")
    if len(atts) > _MAX_ATTACH:
        raise HTTPException(status_code=400, detail=f"Maximum {_MAX_ATTACH} pièces jointes.")

    att_list = [
        {"name": a.name[:255], "url": a.url, "type": a.type[:100], "size": min(a.size, _MAX_FILE)}
        for a in atts
    ]

    flag_reason = _scan_suspicious(body) if body else None

    # Validate reply_to_id
    reply_to_id_val: Optional[UUID] = None
    replied_body_val: Optional[str] = None
    replied_sender_name_val: Optional[str] = None

    if payload.reply_to_id:
        try:
            rid = UUID(payload.reply_to_id)
            replied = db.exec(
                select(ChatMessage).where(
                    ChatMessage.id == rid,
                    ChatMessage.conv_id == conv_id,
                )
            ).first()
            if not replied:
                raise HTTPException(status_code=400, detail="Message de référence introuvable.")
            reply_to_id_val = rid
            replied_body_val = replied.body
            replied_sender = _get_profile(replied.sender_id, db)
            replied_sender_name_val = (replied_sender.full_name or "Utilisateur") if replied_sender else "Utilisateur"
        except HTTPException:
            raise
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="reply_to_id invalide.")

    msg = ChatMessage(
        conv_id=conv_id,
        sender_id=me,
        body=body,
        attachments=att_list or None,
        created_at=datetime.now(timezone.utc),
        is_flagged=flag_reason is not None,
        flag_reason=flag_reason,
        reply_to_id=reply_to_id_val,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)

    if flag_reason:
        logger.warning(
            "Flagged message: id=%s conv=%s sender=%s reason=%r",
            msg.id, conv_id, me, flag_reason,
        )

    event = {
        "type": "new_message",
        "conv_id": str(conv_id),
        "message": {
            "id": str(msg.id),
            "conv_id": str(conv_id),
            "sender_id": str(me),
            "body": msg.body,
            "attachments": att_list,
            "created_at": msg.created_at.isoformat(),
            "read_at": None,
            "is_mine": False,
            "reply_to_id": str(reply_to_id_val) if reply_to_id_val else None,
            "replied_body": replied_body_val,
            "replied_sender_name": replied_sender_name_val,
        },
    }

    # Push to all recipients (not sender — sender has message from HTTP response).
    # Direct push for instant same-worker delivery, Redis for cross-worker + reliability.
    # Frontend deduplicates by message ID, so receiving both paths is harmless.
    from app.core.connections import chat_connections
    sender_profile = _get_profile(me, db)
    sender_name = (sender_profile.full_name if sender_profile else None) or "Utilisateur"
    preview = (body or "📎 Pièce jointe")[:80]
    for pid in _participant_ids(conv_id, db):
        if pid == str(me):
            continue
        await chat_connections.send(pid, event)
        _publish(f"chat:user:{pid}", event)

        recipient_role = _get_role(UUID(pid), db)
        chat_path = {"parent": "/parent/teacher-chat", "teacher": "/teacher/chat"}.get(recipient_role, "/student/chat")

        from app.services.notification_engine import emit
        emit(
            db, event_type="new_message", user_id=UUID(pid),
            context={"sender_name": sender_name, "preview": preview},
            data={"conv_id": str(conv_id)},
            deep_link_override=chat_path,
        )

    return _fmt(msg, me, replied_body_val, replied_sender_name_val)


@router.post("/{conv_id}/read")
async def mark_read(
    conv_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    me = _me(current_user)
    _assert_participant(me, conv_id, db)

    now = datetime.now(timezone.utc)
    part = db.exec(
        select(ConversationParticipant).where(
            ConversationParticipant.conv_id == conv_id,
            ConversationParticipant.user_id == me,
        )
    ).first()
    if part:
        part.last_read_at = now
        db.add(part)
        db.commit()

    read_at_str = now.isoformat()
    mark_event = {
        "type": "message_read",
        "conv_id": str(conv_id),
        "user_id": str(me),
        "read_at": read_at_str,
    }

    from app.core.connections import chat_connections
    for pid in _participant_ids(conv_id, db):
        if pid != str(me):
            delivered = await chat_connections.send(pid, mark_event)
            if not delivered:
                _publish(f"chat:user:{pid}", mark_event)

    return {"ok": True}


@router.delete("/{conv_id}", status_code=status.HTTP_204_NO_CONTENT)
def hide_conversation(
    conv_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Hide a conversation from the requesting user's list.
    The other participant is unaffected. All messages are preserved.
    Re-opening via create_or_get_conversation restores visibility.
    """
    me = _me(current_user)
    _assert_participant(me, conv_id, db)

    part = db.exec(
        select(ConversationParticipant).where(
            ConversationParticipant.conv_id == conv_id,
            ConversationParticipant.user_id == me,
        )
    ).first()
    if part:
        part.hidden_at = datetime.now(timezone.utc)
        db.add(part)
        db.commit()


@router.post("/upload")
async def upload_attachment(
    file: UploadFile = File(...),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    content = await file.read()
    if len(content) > _MAX_FILE:
        raise HTTPException(status_code=413, detail="Fichier trop volumineux (max 10 Mo).")

    from app.services.storage import (
        GENERAL_CONTENT_TYPES, GENERAL_EXTENSIONS, UploadValidationError,
        upload_file, validate_upload,
    )
    try:
        validate_upload(
            filename=file.filename, content_type=file.content_type, size=len(content),
            allowed_content_types=GENERAL_CONTENT_TYPES, allowed_extensions=GENERAL_EXTENSIONS,
            max_size=_MAX_FILE,
        )
    except UploadValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    filename = file.filename or "file"
    url = upload_file(
        content,
        filename,
        content_type=file.content_type or "application/octet-stream",
        folder="chat",
    )

    return {
        "name": filename,
        "url": url,
        "type": file.content_type or "application/octet-stream",
        "size": len(content),
    }


@router.get("/contacts")
def list_contacts(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    me = _me(current_user)
    me_role = current_user.get("role", "student")

    contacts = []

    if me_role in ("student", "parent"):
        from app.models.booking import Booking
        if me_role == "student":
            teacher_ids = list({
                b.teacher_id
                for b in db.exec(select(Booking).where(Booking.student_id == me)).all()
            })
        else:
            from app.models.parent_link import ParentStudentLink
            links = db.exec(
                select(ParentStudentLink).where(ParentStudentLink.parent_id == me)
            ).all()
            student_ids = [lnk.student_id for lnk in links]
            teacher_ids = list({
                b.teacher_id
                for b in db.exec(
                    select(Booking).where(Booking.student_id.in_(student_ids))
                ).all()
            }) if student_ids else []

        for tid in teacher_ids:
            p = _get_profile(tid, db)
            if p:
                contacts.append({
                    "id": str(tid), "name": p.full_name or "Enseignant",
                    "role": "teacher", "avatar": p.avatar_url,
                    "is_online": _is_online(str(tid)),
                })

    elif me_role == "teacher":
        from app.models.booking import Booking
        bookings = db.exec(select(Booking).where(Booking.teacher_id == me)).all()
        seen: set[UUID] = set()

        for b in bookings:
            if b.student_id not in seen:
                seen.add(b.student_id)
                p = _get_profile(b.student_id, db)
                if p:
                    contacts.append({
                        "id": str(b.student_id), "name": p.full_name or "Étudiant",
                        "role": "student", "avatar": p.avatar_url,
                        "is_online": _is_online(str(b.student_id)),
                    })

        from app.models.parent_link import ParentStudentLink
        student_ids = [b.student_id for b in bookings]
        if student_ids:
            parent_links = db.exec(
                select(ParentStudentLink).where(
                    ParentStudentLink.student_id.in_(student_ids)
                )
            ).all()
            parent_seen: set[UUID] = set()
            for lnk in parent_links:
                if lnk.parent_id not in parent_seen:
                    parent_seen.add(lnk.parent_id)
                    p = _get_profile(lnk.parent_id, db)
                    if p:
                        contacts.append({
                            "id": str(lnk.parent_id), "name": p.full_name or "Parent",
                            "role": "parent", "avatar": p.avatar_url,
                            "is_online": _is_online(str(lnk.parent_id)),
                        })

    return contacts
