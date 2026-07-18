from __future__ import annotations

"""
Question Engine — Competitive Arena Phase 1.

Reuses the existing solo-practice question bank (app/models/practice.py —
Question/QuestionChoice) rather than duplicating it; the future gameplay
engine (phase 2+) will pull questions for a live match from here. Phase 1
only exposes a feasibility check: how many approved questions exist for a
proposed match configuration, so the lobby can warn the creator if there
aren't enough before a match ever starts.
"""

from typing import List, Optional
from uuid import UUID

from sqlmodel import Session, func, select

from app.models.practice import Question


def count_available_questions(
    db: Session,
    *,
    subject_ids: List[UUID],
    school_level_id: Optional[UUID],
    difficulty: Optional[str],
) -> int:
    query = select(func.count()).select_from(Question).where(
        Question.active.is_(True),
        Question.validation_status == "approved",
        Question.deleted_at.is_(None),
    )
    if subject_ids:
        query = query.where(Question.subject_id.in_(subject_ids))
    if school_level_id:
        query = query.where(Question.school_level_id == school_level_id)
    if difficulty:
        query = query.where(Question.difficulty == difficulty)
    return db.exec(query).one()
