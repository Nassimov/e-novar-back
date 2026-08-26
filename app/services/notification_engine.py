from __future__ import annotations

"""
Central Notification Engine — the single entrypoint every feature should
call instead of constructing Notification rows or calling onesignal/email
directly. See migration 070_notification_system.sql for the schema this
relies on, and app/services/notification_templates_seed.py for the event
catalogue.

    emit(db, event_type="lesson_booked", user_id=..., context={...})
        1. looks up NotificationTemplate for event_type (category/priority/
           channels/title & body templates)
        2. persists ONE Notification row (in_app) — deduped via dedup_key
           if provided (partial unique index on (user_id, dedup_key))
        3. real-time WS push fires automatically (Notification's
           after_insert hook in app/models/notification.py — already
           reused by the chat channel, no new WS endpoint needed)
        4. enqueues a NotificationQueue row per additional channel
           (push/email) the template calls for AND the user hasn't opted
           out of for that category — processed asynchronously by
           app/workers/notification_tasks.py's task_process_notification_queue

Any future feature that wants to notify a user only needs a row in
notification_templates (or an inline title/body override) and a call to
emit() — it never needs to touch OneSignal, email, or WS directly.
"""

import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlmodel import Session, select

from app.models.notification import (
    ALWAYS_IN_APP_CATEGORIES,
    Notification,
    NotificationPreference,
    NotificationQueue,
    NotificationTemplate,
)
from app.models.profile import UserRole

# Mirrors app/routers/auth.py's GET /me role resolution exactly — a user may
# hold multiple roles, this picks the one their frontend routes actually use.
_ROLE_PRIORITY = {"admin": 4, "teacher": 3, "parent": 2, "student": 1}


def _resolve_role(db: Session, user_id: UUID) -> Optional[str]:
    rows = db.exec(select(UserRole).where(UserRole.user_id == user_id)).all()
    if not rows:
        return None
    return max(rows, key=lambda r: _ROLE_PRIORITY.get(r.role, 0)).role

logger = logging.getLogger(__name__)


def _render(template: str, context: Dict[str, Any]) -> str:
    try:
        return template.format(**context)
    except Exception:
        # Missing placeholder in context — better to show the raw template
        # than to 500 the caller's request over a cosmetic string issue.
        return template


def _render_deep_link(template: str, context: Dict[str, Any]) -> Optional[str]:
    """Unlike _render (used for title/body, where a raw unrendered template
    is a harmless cosmetic fallback), a deep_link with a missing placeholder
    must never fall back to the raw template string — that produces a
    literal, clickable-but-404ing URL like "/{role}/sessions" (see the
    session_validation notification bug this was written to prevent). No
    link at all is strictly better than a broken one; the notification
    itself still renders, it just isn't clickable. Logged as an error
    (not swallowed) since a missing placeholder here is a real bug, not
    routine — most likely a template referencing a context key no caller
    actually provides."""
    try:
        return template.format(**context)
    except Exception:
        logger.error("notification_engine: deep_link_template %r failed to render with context=%s", template, context)
        return None


def _category_prefs(db: Session, user_id: UUID) -> Dict[str, Dict[str, bool]]:
    pref = db.get(NotificationPreference, user_id)
    if pref and pref.category_prefs:
        return pref.category_prefs
    return {}


def channel_allowed(db: Session, *, user_id: UUID, category: str, channel: str, priority: str) -> bool:
    """push/email/in_app all respect the user's per-category preference,
    except critical priority and the always-on categories (account/security),
    which bypass opt-out entirely (even for in_app) — those are things like
    password-changed or suspension alerts a user must always be able to see,
    regardless of what they've toggled off."""
    if priority == "critical" or category in ALWAYS_IN_APP_CATEGORIES:
        return True
    return bool(_category_prefs(db, user_id).get(category, {}).get(channel, True))


def emit(
    db: Session,
    *,
    event_type: str,
    user_id: UUID,
    context: Optional[Dict[str, Any]] = None,
    dedup_key: Optional[str] = None,
    data: Optional[Dict[str, Any]] = None,
    title_override: Optional[str] = None,
    body_override: Optional[str] = None,
    deep_link_override: Optional[str] = None,
) -> Optional[Notification]:
    """
    Returns the persisted in_app Notification, or None if:
      - the event_type has no active template AND no title/body override, or
      - dedup_key collided with an existing notification for this user
        (constraint violation is swallowed — that IS the intended behavior),
      - or ANYTHING else went wrong (see the top-level guard below).

    Notifications are a side effect, never the main point of the request
    that triggers them — a caller accepting a booking, awarding KP, etc.
    must never fail just because a notification template is missing, a
    migration hasn't run yet, the DB hiccuped, or any other notification-only
    problem. Every failure path here is caught, logged, and swallowed.
    """
    try:
        return _emit_inner(
            db, event_type=event_type, user_id=user_id, context=context,
            dedup_key=dedup_key, data=data, title_override=title_override,
            body_override=body_override, deep_link_override=deep_link_override,
        )
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.exception("notification_engine.emit failed for event_type=%s user=%s — swallowed", event_type, user_id)
        return None


def _emit_inner(
    db: Session,
    *,
    event_type: str,
    user_id: UUID,
    context: Optional[Dict[str, Any]] = None,
    dedup_key: Optional[str] = None,
    data: Optional[Dict[str, Any]] = None,
    title_override: Optional[str] = None,
    body_override: Optional[str] = None,
    deep_link_override: Optional[str] = None,
) -> Optional[Notification]:
    context = dict(context) if context else {}
    # `{role}` is by far the most common deep_link_template placeholder
    # (nearly every seeded template routes to "/{role}/..."). Auto-inject it
    # so individual emit()/_notify() call sites never have to remember to —
    # forgetting it isn't a cosmetic issue, it used to leave the raw
    # "/{role}/sessions" literal in the rendered deep_link (see _render's
    # fallback below), which 404s when a user clicks the notification.
    if "role" not in context:
        role = _resolve_role(db, user_id)
        if role:
            context["role"] = role
    template = db.exec(
        select(NotificationTemplate).where(NotificationTemplate.event_type == event_type)
    ).first()

    if template is not None and not template.active:
        return None
    if template is None and not (title_override and body_override):
        logger.warning("notification_engine.emit: no template for event_type=%s and no override given", event_type)
        return None

    category = template.category if template else "system"
    priority = template.priority if template else "normal"
    channels = template.channels if template else ["in_app"]

    title = title_override or _render(template.title_template, context)
    body = body_override or _render(template.body_template, context)
    deep_link = deep_link_override or (
        _render_deep_link(template.deep_link_template, context)
        if template and template.deep_link_template
        else None
    )

    # Each channel is independently opt-in/out — a user may keep push on for
    # a category while turning off its in-app history entry, or vice versa.
    notif: Optional[Notification] = None
    if channel_allowed(db, user_id=user_id, category=category, channel="in_app", priority=priority):
        notif = Notification(
            user_id=user_id,
            type=event_type,
            category=category,
            priority=priority,
            title=title,
            body=body,
            data=data or context,
            deep_link=deep_link,
            dedup_key=dedup_key,
            channel="in_app",
        )
        db.add(notif)
        try:
            db.commit()
        except Exception:
            db.rollback()
            logger.debug("emit(%s) deduped for user=%s dedup_key=%s", event_type, user_id, dedup_key)
            return None
        db.refresh(notif)

    queued_any = False
    for channel in channels:
        if channel == "in_app":
            continue
        if not channel_allowed(db, user_id=user_id, category=category, channel=channel, priority=priority):
            continue
        db.add(NotificationQueue(
            event_type=event_type,
            user_id=user_id,
            context={
                "notification_id": str(notif.id) if notif else None,
                "channel": channel,
                "title": title,
                "body": body,
                "deep_link": deep_link,
                "data": data or context,
            },
            dedup_key=f"{dedup_key}:{channel}" if dedup_key else None,
        ))
        queued_any = True
    if queued_any:
        try:
            db.commit()
        except Exception:
            db.rollback()
            logger.warning("emit(%s) queue insert failed for user=%s", event_type, user_id)
        else:
            from app.workers.notification_tasks import task_process_notification_queue
            task_process_notification_queue.delay()

    return notif


def emit_bulk(
    db: Session,
    *,
    event_type: str,
    user_ids: List[UUID],
    context: Optional[Dict[str, Any]] = None,
    dedup_key: Optional[str] = None,
) -> int:
    """Same event to many users at once (e.g. every participant of a group
    lesson). Returns how many in_app rows were actually persisted (excludes
    dedup collisions)."""
    count = 0
    for uid in user_ids:
        if emit(db, event_type=event_type, user_id=uid, context=context, dedup_key=dedup_key) is not None:
            count += 1
    return count
