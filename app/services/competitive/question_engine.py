from __future__ import annotations

"""
Question Engine — Competitive Arena.

Reuses the existing solo-practice question bank (app/models/practice.py —
Question/QuestionChoice) rather than duplicating it. Phase 1/2 only exposed
a feasibility check (how many approved questions exist for a proposed match
configuration); Phase 3 adds the actual real-time selection used to build a
match's question list (see select_questions_for_match below).

Only single-choice questions are selectable (Phase 3 spec: "Only Single
Choice is enabled now") — is_multi_choice=False is filtered everywhere here
so the wizard's "available questions" estimate never overstates what
gameplay can actually draw from. The `type` column stays free-text so future
question types (image/formula/ordering/matching/code/fill-blank) need no
schema change, only a new value + a new frontend renderer.
"""

import random
from typing import Dict, List, Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlmodel import Session, func, select

from app.models.competitive import CompetitiveMatchQuestion, CompetitiveQuestionAttempt
from app.models.practice import Question, QuestionChoice


def _base_query(
    *, subject_ids: List[UUID], school_level_id: Optional[UUID], difficulty: Optional[str],
):
    query = select(Question).where(
        Question.active.is_(True),
        Question.validation_status == "approved",
        Question.deleted_at.is_(None),
        Question.is_multi_choice.is_(False),
    )
    if subject_ids:
        query = query.where(Question.subject_id.in_(subject_ids))
    if school_level_id:
        query = query.where(Question.school_level_id == school_level_id)
    if difficulty:
        query = query.where(Question.difficulty == difficulty)
    return query


def count_available_questions(
    db: Session,
    *,
    subject_ids: List[UUID],
    school_level_id: Optional[UUID],
    difficulty: Optional[str],
) -> int:
    query = select(func.count()).select_from(
        _base_query(subject_ids=subject_ids, school_level_id=school_level_id, difficulty=difficulty).subquery()
    )
    return db.exec(query).one()


#: Used when no matching question rows exist yet to average a real
#: estimated_time_sec from (e.g. a brand new subject/level combo).
_DEFAULT_SEC_PER_QUESTION = 30
#: Buffer for reading the result screen / transition between questions —
#: keeps the estimate honest for scheduling/conflict-detection purposes.
_REVIEW_BUFFER_RATIO = 0.2


def estimate_match_duration_minutes(
    db: Session,
    *,
    subject_ids: List[UUID],
    school_level_id: Optional[UUID],
    difficulty: Optional[str],
    question_count: int,
) -> int:
    """Real estimate driven by the question bank's own estimated_time_sec —
    never a hardcoded flat constant — used both for the challenge wizard's
    "estimated duration" summary and for scheduling conflict detection."""
    base = _base_query(subject_ids=subject_ids, school_level_id=school_level_id, difficulty=difficulty)
    query = select(func.avg(Question.estimated_time_sec)).select_from(base.subquery())
    avg_sec = db.exec(query).one() or _DEFAULT_SEC_PER_QUESTION
    total_sec = float(avg_sec) * question_count * (1 + _REVIEW_BUFFER_RATIO)
    return max(1, round(total_sec / 60))


def select_questions_for_match(
    db: Session,
    *,
    subject_ids: List[UUID],
    school_level_id: Optional[UUID],
    difficulty: Optional[str],
    question_count: int,
    player_ids: List[UUID],
) -> List[Question]:
    """Real, non-hardcoded selection for a match's authoritative question
    list (called once at match start — see gameplay_service.start_match).

    - Hard requirement: every returned question is active/approved/single-
      choice/has >=2 choices with >=1 correct — anything failing this is
      dropped, never surfaced to a match.
    - Soft preference: avoid questions any of the players answered in a
      recent competitive match — applied only if enough non-recent
      candidates remain, so a small question bank never blocks a match.
    - Hard requirement: no duplicate questions inside the same match.
    """
    candidates = list(
        db.exec(_base_query(subject_ids=subject_ids, school_level_id=school_level_id, difficulty=difficulty)).all()
    )
    if not candidates:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Aucune question disponible pour cette configuration.")

    candidate_ids = [q.id for q in candidates]
    choice_counts = db.exec(
        select(
            QuestionChoice.question_id,
            func.count().label("total"),
            func.count().filter(QuestionChoice.is_correct.is_(True)).label("correct"),
        )
        .where(QuestionChoice.question_id.in_(candidate_ids))
        .group_by(QuestionChoice.question_id)
    ).all()
    valid_ids = {row.question_id for row in choice_counts if row.total >= 2 and row.correct >= 1}
    candidates = [q for q in candidates if q.id in valid_ids]

    if len(candidates) < question_count:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Pas assez de questions valides disponibles ({len(candidates)}/{question_count}).",
        )

    recent_ids: set = set()
    if player_ids:
        rows = db.exec(
            select(CompetitiveMatchQuestion.question_id)
            .join(CompetitiveQuestionAttempt, CompetitiveQuestionAttempt.match_question_id == CompetitiveMatchQuestion.id)
            .where(CompetitiveQuestionAttempt.user_id.in_(player_ids))
            .order_by(CompetitiveQuestionAttempt.submitted_at.desc())
            .limit(200)
        ).all()
        recent_ids = set(rows)

    preferred = [q for q in candidates if q.id not in recent_ids]
    pool = preferred if len(preferred) >= question_count else candidates

    selected = random.sample(pool, question_count)
    return selected


def get_choices_for_questions(db: Session, question_ids: List[UUID]) -> Dict[UUID, List[QuestionChoice]]:
    if not question_ids:
        return {}
    rows = db.exec(select(QuestionChoice).where(QuestionChoice.question_id.in_(question_ids))).all()
    result: Dict[UUID, List[QuestionChoice]] = {qid: [] for qid in question_ids}
    for choice in rows:
        result.setdefault(choice.question_id, []).append(choice)
    return result
