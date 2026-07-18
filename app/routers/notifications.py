from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.notification import NOTIFICATION_CATEGORIES, Notification, NotificationPreference
from app.models.profile import Profile
from app.schemas.notification import CategoryPrefToggle, NotificationPrefsUpdate, NotificationResponse

router = APIRouter(tags=["Notifications"])


class ReadByTypeIn(BaseModel):
    types: list[str]


@router.get("/unread-summary")
async def get_unread_summary(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Unread notification count grouped by `type` — feeds the sidebar's
    per-menu 'new activity' blink indicators (see src/lib/badge-store.ts).
    Initial/poll-fallback load; live updates arrive over the /ws/messages
    channel as `{"type": "notification", "notification_type": ...}` events.

    Excludes 'new_message' — that's already covered by the Messages nav
    item's own numeric unread-count pill (see /api/messages/unread-count);
    counting it here too would blink the generic Notifications bell for
    every chat message on top of the dedicated pill."""
    user_id = UUID(current_user["id"])
    unread = db.exec(
        select(Notification).where(
            Notification.user_id == user_id,
            Notification.read_at == None,  # noqa: E711
            Notification.type != "new_message",
        )
    ).all()
    by_type: Dict[str, int] = {}
    for n in unread:
        by_type[n.type] = by_type.get(n.type, 0) + 1
    return {"unread_total": len(unread), "by_type": by_type}


@router.patch("/read-by-type")
async def mark_notifications_read_by_type(
    payload: ReadByTypeIn,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mark all unread notifications of the given type(s) as read — called
    when the user clicks a sidebar menu item mapped to those types, so the
    blink doesn't come back on the next page load."""
    if not payload.types:
        return {"message": "Marked 0 notifications as read"}
    user_id = UUID(current_user["id"])
    notifications = db.exec(
        select(Notification).where(
            Notification.user_id == user_id,
            Notification.read_at == None,  # noqa: E711
            Notification.type.in_(payload.types),
        )
    ).all()
    now = datetime.now(timezone.utc)
    for n in notifications:
        n.read_at = now
        db.add(n)
    db.commit()
    return {"message": f"Marked {len(notifications)} notifications as read"}


@router.get("/", response_model=Dict)
async def list_notifications(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    unread_only: bool = Query(False),
    archived: bool = Query(False, description="True = only archived; False = only active (default)"),
    category: str = Query(None),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List notifications for the current user, newest first. By default
    only active (non-archived) notifications — pass archived=true for the
    'archived' tab in the notification center."""
    user_id = UUID(current_user["id"])

    query = select(Notification).where(Notification.user_id == user_id)
    if archived:
        query = query.where(Notification.archived_at.is_not(None))
    else:
        query = query.where(Notification.archived_at.is_(None))
    if unread_only:
        query = query.where(Notification.read_at == None)  # noqa: E711
    if category:
        query = query.where(Notification.category == category)
    query = query.order_by(Notification.created_at.desc())

    notifications = db.exec(query).all()
    total = len(notifications)
    offset = (page - 1) * size
    paginated = notifications[offset : offset + size]

    return {
        "items": [NotificationResponse.model_validate(n) for n in paginated],
        "total": total,
        "unread_count": sum(1 for n in notifications if n.read_at is None),
        "page": page,
        "size": size,
        "pages": math.ceil(total / size) if total else 0,
    }


@router.patch("/{notification_id}/archive")
async def archive_notification(
    notification_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Archive a notification — removed from the default view, still
    reachable via the 'archived' filter (never deleted)."""
    user_id = UUID(current_user["id"])
    notification = db.get(Notification, notification_id)
    if notification is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    if notification.user_id != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    notification.archived_at = datetime.now(timezone.utc)
    db.add(notification)
    db.commit()
    return {"message": "Archived"}


@router.patch("/{notification_id}/unarchive")
async def unarchive_notification(
    notification_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    notification = db.get(Notification, notification_id)
    if notification is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    if notification.user_id != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    notification.archived_at = None
    db.add(notification)
    db.commit()
    return {"message": "Unarchived"}


@router.delete("/{notification_id}", status_code=204)
async def delete_notification(
    notification_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    notification = db.get(Notification, notification_id)
    if notification is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    if notification.user_id != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    db.delete(notification)
    db.commit()


@router.patch("/{notification_id}/read")
async def mark_notification_read(
    notification_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mark a single notification as read."""
    user_id = UUID(current_user["id"])
    notification = db.get(Notification, notification_id)
    if notification is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    if notification.user_id != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    notification.read_at = datetime.now(timezone.utc)
    db.add(notification)
    db.commit()
    return {"message": "Marked as read"}


@router.patch("/read-all")
async def mark_all_notifications_read(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mark all unread notifications as read."""
    user_id = UUID(current_user["id"])
    notifications = db.exec(
        select(Notification).where(
            Notification.user_id == user_id,
            Notification.read_at == None,  # noqa: E711
        )
    ).all()
    now = datetime.now(timezone.utc)
    for n in notifications:
        n.read_at = now
        db.add(n)
    db.commit()
    return {"message": f"Marked {len(notifications)} notifications as read"}


@router.get("/preferences")
async def get_notification_preferences(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get the current user's notification preferences."""
    user_id = UUID(current_user["id"])
    prefs = db.get(NotificationPreference, user_id)
    if prefs is None:
        raise HTTPException(status_code=404, detail="Preferences not found")
    return prefs


@router.patch("/preferences/category")
async def toggle_category_preference(
    payload: CategoryPrefToggle,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Flip one (category, channel) switch — e.g. disable push for 'chat'.
    This is what app/services/notification_engine.py's channel_allowed()
    actually consults for push/email fan-out."""
    if payload.category not in NOTIFICATION_CATEGORIES:
        raise HTTPException(status_code=422, detail=f"Unknown category. Must be one of {NOTIFICATION_CATEGORIES}")
    if payload.channel not in ("push", "email", "in_app"):
        raise HTTPException(status_code=422, detail="channel must be push, email, or in_app")

    user_id = UUID(current_user["id"])
    prefs = db.get(NotificationPreference, user_id)
    if prefs is None:
        prefs = NotificationPreference(user_id=user_id)

    matrix = dict(prefs.category_prefs or {})
    matrix[payload.category] = {**matrix.get(payload.category, {}), payload.channel: payload.enabled}
    prefs.category_prefs = matrix
    db.add(prefs)
    db.commit()
    db.refresh(prefs)
    return prefs


@router.put("/preferences")
async def update_notification_preferences(
    payload: NotificationPrefsUpdate,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update the current user's notification preferences."""
    user_id = UUID(current_user["id"])
    prefs = db.get(NotificationPreference, user_id)
    if prefs is None:
        prefs = NotificationPreference(user_id=user_id)
        db.add(prefs)

    for field, value in payload.model_dump().items():
        setattr(prefs, field, value)
    db.add(prefs)
    db.commit()
    db.refresh(prefs)
    return prefs
