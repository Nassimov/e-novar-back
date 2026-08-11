from __future__ import annotations

"""Competitive Arena — Lobby API (Phase 1: ready-state only, no gameplay)."""

from typing import Any, Dict
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session

from app.core.competitive_ws import publish_competitive_event
from app.dependencies import get_current_user, get_db
from app.models.admin import PlatformSettings
from app.routers.competitive.matches import _to_match_response
from app.schemas.competitive import MatchResponse
from app.services.competitive import event_log_service, match_service
from app.services.notification_engine import emit

router = APIRouter(tags=["Competitive"])


class ReadyRequest(BaseModel):
    ready: bool = True


@router.get("/matches/{match_id}/lobby", response_model=MatchResponse)
def get_lobby(
    match_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    match = match_service.get_match_or_404(db, match_id)
    participants = match_service.get_participants(db, match_id)
    match_service.assert_can_view(match, participants, user_id)
    return _to_match_response(db, match)


@router.post("/matches/{match_id}/enter-waiting-room", response_model=MatchResponse)
def enter_waiting_room(
    match_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Either player's 'Play now' choice after acceptance, or manually
    joining once a scheduled match's slot is at hand."""
    user_id = UUID(current_user["id"])
    match = match_service.get_match_or_404(db, match_id)
    match = match_service.enter_waiting_room(db, match, user_id=user_id)

    event_log_service.log_event(db, match_id=match.id, actor_id=user_id, event_type="waiting_room_entered", meta={})
    publish_competitive_event(str(match.id), {"type": "ready_changed", "user_id": str(user_id), "ready": False, "match_status": match.status})
    for participant in match_service.get_participants(db, match.id):
        emit(
            db, event_type="competitive_waiting_room_open", user_id=participant.user_id,
            dedup_key=f"competitive_waiting_room_open:{match.id}:{participant.user_id}",
        )
    return _to_match_response(db, match)


@router.post("/matches/{match_id}/lobby/ready", response_model=MatchResponse)
def set_ready(
    match_id: UUID,
    payload: ReadyRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    match = match_service.get_match_or_404(db, match_id)
    match = match_service.set_ready(db, match, user_id=user_id, ready=payload.ready)

    event_log_service.log_event(
        db, match_id=match.id, actor_id=user_id, event_type="ready_toggled",
        meta={"ready": payload.ready},
    )
    publish_competitive_event(str(match.id), {
        "type": "ready_changed", "user_id": str(user_id), "ready": payload.ready, "match_status": match.status,
    })
    if payload.ready:
        participants = match_service.get_participants(db, match.id)
        other = next((p for p in participants if p.user_id != user_id), None)
        if other:
            emit(
                db, event_type="competitive_opponent_ready", user_id=other.user_id,
                dedup_key=f"competitive_opponent_ready:{match.id}:{user_id}",
            )

    if match.status == "countdown":
        # Both players just readied up — schedule the actual gameplay start
        # (question generation, etc.) after the admin-configured pre-match
        # countdown (see app/services/competitive/gameplay_service.py). If
        # this dispatch is ever dropped (broker hiccup), compute_state()'s
        # own self-heal check catches it the moment a client next asks for
        # match state — never permanently stuck.
        try:
            settings_row = db.get(PlatformSettings, True) or PlatformSettings()
            from app.workers.competitive_tasks import task_begin_match_after_countdown
            task_begin_match_after_countdown.apply_async(
                args=[str(match.id)], countdown=settings_row.competitive_match_countdown_seconds,
            )
        except Exception:
            pass

    return _to_match_response(db, match)
