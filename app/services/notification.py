from __future__ import annotations

"""
Unified notification service — single entry point for all notification types.

Stack:
  Push notifications  → OneSignal
  Emails (transac)    → OneSignal (include_email_tokens)
  Emails (campaigns)  → OneSignal (included_segments + target_channel=email)
  SMS (critique only) → Twilio via Celery

Call order for any event:
  1. persist()  → writes a row to public.notifications (in-app, always)
  2. push()     → OneSignal push (respects user prefs)
  3. email_*()  → OneSignal email via Celery (respects user prefs)
  4. sms_*()    → Twilio via Celery (critical events only)

Callers import only this module — they never call onesignal/twilio directly.
"""

import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlmodel import Session

from app.models.notification import Notification, NotificationPreference
from app.services import onesignal

logger = logging.getLogger(__name__)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_prefs(user_id: UUID, db: Session) -> NotificationPreference:
    prefs = db.get(NotificationPreference, user_id)
    return prefs if prefs is not None else NotificationPreference(user_id=user_id)


# ─── Persist (in-app) ────────────────────────────────────────────────────────

def persist(
    db: Session,
    *,
    user_id: UUID,
    type: str,
    title: str,
    body: str,
    channel: str = "in_app",
    data: Optional[Dict[str, Any]] = None,
) -> Notification:
    """Write a notification row to the DB. Always called regardless of user prefs."""
    notif = Notification(
        user_id=user_id,
        type=type,
        title=title,
        body=body,
        channel=channel,
        data=data or {},
    )
    db.add(notif)
    db.commit()
    db.refresh(notif)
    return notif


# ─── Push ─────────────────────────────────────────────────────────────────────

def push(
    db: Session,
    *,
    user_id: UUID,
    title: str,
    body: str,
    data: Optional[Dict[str, Any]] = None,
) -> None:
    """Send a push to one user if they have push enabled."""
    if not _get_prefs(user_id, db).push:
        return
    onesignal.send_push(user_ids=[str(user_id)], title=title, body=body, data=data)


def push_many(user_ids: List[UUID], title: str, body: str, data: Optional[Dict[str, Any]] = None) -> None:
    """Broadcast push to multiple users (no prefs check)."""
    onesignal.send_push(user_ids=[str(uid) for uid in user_ids], title=title, body=body, data=data)


def push_campaign(segment: str, title: str, body: str, data: Optional[Dict[str, Any]] = None) -> None:
    """Push campaign to a segment (All / students / teachers / parents)."""
    onesignal.send_push_to_segment(segment, title, body, data)


# ─── Email (transactional via OneSignal) ─────────────────────────────────────

def email_welcome(to: str, name: str) -> None:
    from app.workers.email_tasks import send_welcome_email
    send_welcome_email.delay(to, name)


def email_booking_confirmed(to: str, booking_data: Dict[str, Any]) -> None:
    from app.workers.email_tasks import send_booking_confirmation
    send_booking_confirmation.delay(to, booking_data)


def email_session_reminder(to: str, name: str, teacher_name: str, date_str: str, time_str: str) -> None:
    from app.workers.email_tasks import send_session_reminder_email
    send_session_reminder_email.delay(to, name, teacher_name, date_str, time_str)


def email_withdrawal(to: str, name: str, amount: int, status: str) -> None:
    from app.workers.email_tasks import send_withdrawal_processed_email
    send_withdrawal_processed_email.delay(to, name, amount, status)


# ─── Email (campaigns / automations via OneSignal) ────────────────────────────

def email_campaign(segment: str, subject: str, html: str) -> None:
    """
    Send a marketing email campaign to a OneSignal segment.
    segment: 'All' | 'students' | 'teachers' | 'parents'
    Users must have been registered via onesignal.register_user() to appear in segments.
    """
    from app.workers.notification_tasks import task_email_campaign
    task_email_campaign.delay(segment, subject, html)


# ─── SMS (Twilio, critical only) ──────────────────────────────────────────────

def sms_booking_confirmed(to_phone: str, teacher_name: str, date_str: str, time_str: str) -> None:
    from app.workers.sms_tasks import send_booking_sms
    send_booking_sms.delay(to_phone, teacher_name, date_str, time_str)


def sms_session_reminder(to_phone: str, teacher_name: str, time_str: str) -> None:
    from app.workers.sms_tasks import send_session_reminder_sms
    send_session_reminder_sms.delay(to_phone, teacher_name, time_str)


# ─── High-level event fan-outs ────────────────────────────────────────────────

def notify_booking_confirmed(
    db: Session,
    *,
    user_id: UUID,
    email: Optional[str],
    phone: Optional[str],
    booking_data: Dict[str, Any],
) -> None:
    """in-app + push + email + sms (if phone) on booking confirmation."""
    persist(
        db,
        user_id=user_id,
        type="booking_confirmed",
        title="Session confirmée",
        body=f"Votre session du {booking_data.get('date', '')} est confirmée.",
        channel="in_app",
        data={"booking_id": str(booking_data.get("booking_id", ""))},
    )
    push(db, user_id=user_id, title="Session confirmée", body="Votre réservation est confirmée ✓")
    if email:
        email_booking_confirmed(email, booking_data)
    if phone:
        sms_booking_confirmed(phone, booking_data.get("teacher_name", ""), booking_data.get("date", ""), booking_data.get("slot_time", ""))


def notify_session_reminder(
    db: Session,
    *,
    user_id: UUID,
    email: Optional[str],
    phone: Optional[str],
    name: str,
    teacher_name: str,
    date_str: str,
    time_str: str,
) -> None:
    """in-app + push + email + sms (if phone) 24h before a session."""
    persist(
        db,
        user_id=user_id,
        type="session_reminder",
        title="Rappel de session",
        body=f"Votre session avec {teacher_name} est demain à {time_str}.",
        channel="in_app",
    )
    push(db, user_id=user_id, title="Rappel de session", body=f"Demain à {time_str} avec {teacher_name}.")
    if email:
        email_session_reminder(email, name, teacher_name, date_str, time_str)
    if phone:
        sms_session_reminder(phone, teacher_name, time_str)
