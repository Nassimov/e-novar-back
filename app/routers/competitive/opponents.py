from __future__ import annotations

"""Competitive Arena — Opponent search + blocking API (Phase 2)."""

from typing import Any, Dict, List
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlmodel import Session

from app.dependencies import get_current_user, get_db
from app.services.competitive import blocking_service, opponent_service

router = APIRouter(tags=["Competitive"])


@router.get("/opponents/search")
def search_opponents(
    q: str = Query(..., min_length=1),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    return opponent_service.search_students(db, query=q, current_user_id=user_id)


@router.post("/opponents/{user_id}/block", status_code=status.HTTP_204_NO_CONTENT)
def block_opponent(
    user_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    blocking_service.block_user(db, blocker_id=UUID(current_user["id"]), blocked_id=user_id)


@router.delete("/opponents/{user_id}/block", status_code=status.HTTP_204_NO_CONTENT)
def unblock_opponent(
    user_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    blocking_service.unblock_user(db, blocker_id=UUID(current_user["id"]), blocked_id=user_id)


@router.get("/opponents/blocked")
def list_blocked_opponents(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[str]:
    blocks = blocking_service.list_blocked(db, blocker_id=UUID(current_user["id"]))
    return [str(b.blocked_id) for b in blocks]
