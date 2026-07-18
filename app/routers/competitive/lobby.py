from __future__ import annotations

"""Competitive Arena — Lobby API (Phase 1: ready-state only, no gameplay)."""

from typing import Any, Dict
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session

from app.dependencies import get_current_user, get_db
from app.routers.competitive.matches import _to_match_response
from app.schemas.competitive import MatchResponse
from app.services.competitive import match_service

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
    return _to_match_response(db, match)
