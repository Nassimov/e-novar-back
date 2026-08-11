from __future__ import annotations

"""
Blocking Service — Competitive Arena Phase 2.

Uses the generic app/models/social.py::UserBlock table (not competitive-
specific) — blocking is a cross-feature concept, this is just its first
consumer.
"""

from typing import List
from uuid import UUID

from fastapi import HTTPException, status
from sqlmodel import Session, or_, select

from app.models.social import UserBlock


def block_user(db: Session, *, blocker_id: UUID, blocked_id: UUID) -> UserBlock:
    if blocker_id == blocked_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Impossible de te bloquer toi-même.")
    existing = db.exec(
        select(UserBlock).where(UserBlock.blocker_id == blocker_id).where(UserBlock.blocked_id == blocked_id)
    ).first()
    if existing:
        return existing
    block = UserBlock(blocker_id=blocker_id, blocked_id=blocked_id)
    db.add(block)
    db.commit()
    db.refresh(block)
    return block


def unblock_user(db: Session, *, blocker_id: UUID, blocked_id: UUID) -> None:
    block = db.exec(
        select(UserBlock).where(UserBlock.blocker_id == blocker_id).where(UserBlock.blocked_id == blocked_id)
    ).first()
    if block:
        db.delete(block)
        db.commit()


def list_blocked(db: Session, *, blocker_id: UUID) -> List[UserBlock]:
    return list(db.exec(select(UserBlock).where(UserBlock.blocker_id == blocker_id)).all())


def is_blocked_either_way(db: Session, *, user_a: UUID, user_b: UUID) -> bool:
    return db.exec(
        select(UserBlock).where(
            or_(
                (UserBlock.blocker_id == user_a) & (UserBlock.blocked_id == user_b),
                (UserBlock.blocker_id == user_b) & (UserBlock.blocked_id == user_a),
            )
        )
    ).first() is not None


def blocked_user_ids(db: Session, *, user_id: UUID) -> List[UUID]:
    """Every user_id that either blocked `user_id` or was blocked by them —
    used to exclude both directions from opponent search results."""
    rows = db.exec(
        select(UserBlock).where(or_(UserBlock.blocker_id == user_id, UserBlock.blocked_id == user_id))
    ).all()
    ids = set()
    for r in rows:
        ids.add(r.blocker_id)
        ids.add(r.blocked_id)
    ids.discard(user_id)
    return list(ids)
