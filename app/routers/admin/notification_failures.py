"""Admin visibility into notification_failures — terminal delivery failures
after task_process_notification_queue's retry budget is exhausted (see
app/workers/notification_tasks.py's _MAX_QUEUE_ATTEMPTS). Previously this
table existed but had zero admin-facing surface — failures accumulated
invisibly (see the notifications audit's Phase 2 acceptance criterion:
"échecs visibles dans un tableau de bord").
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from app.dependencies import get_admin_user, get_db
from app.models.notification import NotificationFailure
from app.models.profile import Profile

router = APIRouter(tags=["Admin — Notification Failures"])


@router.get("/")
def list_notification_failures(
    resolved: Optional[bool] = Query(None),
    channel: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    query = select(NotificationFailure)
    if resolved is not None:
        query = query.where(NotificationFailure.resolved == resolved)
    if channel:
        query = query.where(NotificationFailure.channel == channel)
    query = query.order_by(NotificationFailure.created_at.desc()).offset(offset).limit(limit)
    rows = db.exec(query).all()

    user_ids = [r.user_id for r in rows if r.user_id]
    profiles = db.exec(select(Profile).where(Profile.id.in_(user_ids))).all() if user_ids else []
    profile_map = {p.id: p for p in profiles}

    items: List[Dict[str, Any]] = []
    for r in rows:
        profile = profile_map.get(r.user_id) if r.user_id else None
        items.append({
            "id": str(r.id),
            "queue_id": str(r.queue_id) if r.queue_id else None,
            "notification_id": str(r.notification_id) if r.notification_id else None,
            "user_id": str(r.user_id) if r.user_id else None,
            "user_name": profile.full_name if profile else None,
            "user_email": profile.email if profile else None,
            "channel": r.channel,
            "error": r.error,
            "retry_count": r.retry_count,
            "resolved": r.resolved,
            "created_at": r.created_at.isoformat(),
        })

    total_unresolved = db.exec(
        select(NotificationFailure).where(NotificationFailure.resolved == False)  # noqa: E712
    ).all()
    return {"items": items, "total_unresolved": len(total_unresolved)}


@router.post("/{failure_id}/resolve")
def resolve_notification_failure(
    failure_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    failure = db.get(NotificationFailure, failure_id)
    if failure is None:
        raise HTTPException(status_code=404, detail="Notification failure not found")
    failure.resolved = True
    db.add(failure)
    db.commit()
    return {"status": "resolved", "id": str(failure_id)}
