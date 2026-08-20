from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db, require_role
from app.models.homework import Homework, HomeworkGrade, HomeworkStatus, HomeworkSubmission
from app.models.profile import Profile
from app.models.user import User
from app.schemas.homework import (
    HomeworkCreate,
    HomeworkGradeRequest,
    HomeworkResponse,
    HomeworkSubmitRequest,
)
from app.services.kp import award_kp, KpSource

router = APIRouter(tags=["homework"])


def _parse_hints(homework: Homework) -> List[str]:
    try:
        return json.loads(homework.hints or "[]")
    except Exception:
        return []


@router.get("/", response_model=Dict)
async def list_homework(
    status_filter: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List homework assignments for the current user."""
    user = db.get(Profile, UUID(current_user["id"]))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    role = current_user.get("role", "student")
    if role == "teacher":
        query = select(Homework).where(Homework.teacher_id == user.id)
    else:
        query = select(Homework).where(Homework.student_id == user.id)

    if status_filter:
        try:
            query = query.where(Homework.status == HomeworkStatus(status_filter))
        except ValueError:
            pass

    homeworks = db.exec(query.order_by(Homework.created_at.desc())).all()
    total = len(homeworks)
    offset = (page - 1) * size
    paginated = homeworks[offset: offset + size]

    return {
        "items": [
            HomeworkResponse(
                id=h.id, teacher_id=h.teacher_id, student_id=h.student_id,
                session_id=h.session_id, subject=h.subject, title=h.title,
                statement=h.statement, hints=_parse_hints(h), due_at=h.due_at,
                status=h.status.value, kp_reward=h.kp_reward,
                created_at=h.created_at, updated_at=h.updated_at,
            )
            for h in paginated
        ],
        "total": total,
        "page": page,
        "size": size,
        "pages": math.ceil(total / size) if total else 0,
    }


@router.get("/{homework_id}", response_model=HomeworkResponse)
async def get_homework(
    homework_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get a specific homework assignment."""
    hw = db.get(Homework, homework_id)
    if hw is None:
        raise HTTPException(status_code=404, detail="Homework not found")

    user = db.get(Profile, UUID(current_user["id"]))
    if user and hw.student_id != user.id and hw.teacher_id != user.id:
        if current_user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Access denied")

    return HomeworkResponse(
        id=hw.id, teacher_id=hw.teacher_id, student_id=hw.student_id,
        session_id=hw.session_id, subject=hw.subject, title=hw.title,
        statement=hw.statement, hints=_parse_hints(hw), due_at=hw.due_at,
        status=hw.status.value, kp_reward=hw.kp_reward,
        created_at=hw.created_at, updated_at=hw.updated_at,
    )


@router.post("/", response_model=HomeworkResponse, status_code=status.HTTP_201_CREATED)
async def create_homework(
    payload: HomeworkCreate,
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Create a homework assignment (teacher only)."""
    from app.models.booking import TutoringSession

    teacher = db.get(Profile, UUID(current_user["id"]))
    if teacher is None:
        raise HTTPException(status_code=404, detail="Teacher not found")

    has_relationship = db.exec(
        select(TutoringSession)
        .where(
            TutoringSession.teacher_id == teacher.id,
            TutoringSession.student_id == payload.student_id,
        )
        .limit(1)
    ).first()
    if has_relationship is None:
        raise HTTPException(
            status_code=403,
            detail="Vous ne pouvez assigner un devoir qu'à un élève avec qui vous avez une séance.",
        )

    hw = Homework(
        teacher_id=teacher.id,
        student_id=payload.student_id,
        session_id=payload.session_id,
        subject=payload.subject,
        title=payload.title,
        statement=payload.statement,
        hints=json.dumps(payload.hints),
        due_at=payload.due_at,
        kp_reward=payload.kp_reward,
    )
    db.add(hw)
    db.commit()
    db.refresh(hw)

    from app.services.notification_engine import emit
    emit(
        db, event_type="homework_assigned", user_id=hw.student_id,
        context={"teacher_name": teacher.full_name or "Ton professeur", "title": hw.title},
        data={"homework_id": str(hw.id)},
        dedup_key=f"homework_assigned:{hw.id}",
    )

    return HomeworkResponse(
        id=hw.id, teacher_id=hw.teacher_id, student_id=hw.student_id,
        session_id=hw.session_id, subject=hw.subject, title=hw.title,
        statement=hw.statement, hints=_parse_hints(hw), due_at=hw.due_at,
        status=hw.status.value, kp_reward=hw.kp_reward,
        created_at=hw.created_at, updated_at=hw.updated_at,
    )


@router.put("/{homework_id}", response_model=HomeworkResponse)
async def update_homework(
    homework_id: UUID,
    payload: HomeworkCreate,
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Update a homework assignment."""
    from datetime import datetime

    hw = db.get(Homework, homework_id)
    if hw is None:
        raise HTTPException(status_code=404, detail="Homework not found")

    user = db.get(Profile, UUID(current_user["id"]))
    if user is None or hw.teacher_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    hw.subject = payload.subject
    hw.title = payload.title
    hw.statement = payload.statement
    hw.hints = json.dumps(payload.hints)
    hw.due_at = payload.due_at
    hw.kp_reward = payload.kp_reward
    hw.updated_at = datetime.utcnow()
    db.add(hw)
    db.commit()
    db.refresh(hw)

    return HomeworkResponse(
        id=hw.id, teacher_id=hw.teacher_id, student_id=hw.student_id,
        session_id=hw.session_id, subject=hw.subject, title=hw.title,
        statement=hw.statement, hints=_parse_hints(hw), due_at=hw.due_at,
        status=hw.status.value, kp_reward=hw.kp_reward,
        created_at=hw.created_at, updated_at=hw.updated_at,
    )


@router.post("/{homework_id}/submit")
async def submit_homework(
    homework_id: UUID,
    payload: HomeworkSubmitRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Submit a homework answer (student only)."""
    from datetime import datetime

    hw = db.get(Homework, homework_id)
    if hw is None:
        raise HTTPException(status_code=404, detail="Homework not found")

    user = db.get(Profile, UUID(current_user["id"]))
    if user is None or hw.student_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    if hw.status != HomeworkStatus.todo:
        raise HTTPException(status_code=400, detail="Homework already submitted")

    submission = HomeworkSubmission(
        homework_id=homework_id,
        text=payload.text,
        attachment_url=payload.attachment_url,
    )
    db.add(submission)

    hw.status = HomeworkStatus.submitted
    hw.updated_at = datetime.utcnow()
    db.add(hw)
    db.commit()
    return {"message": "Homework submitted successfully"}


@router.post("/{homework_id}/grade")
async def grade_homework(
    homework_id: UUID,
    payload: HomeworkGradeRequest,
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Grade a submitted homework (teacher only)."""
    from datetime import datetime

    hw = db.get(Homework, homework_id)
    if hw is None:
        raise HTTPException(status_code=404, detail="Homework not found")

    user = db.get(Profile, UUID(current_user["id"]))
    if user is None or hw.teacher_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    if hw.status != HomeworkStatus.submitted:
        raise HTTPException(status_code=400, detail="Homework has not been submitted yet")

    # Never let a teacher-supplied kp_awarded exceed the reward promised at
    # assignment time — otherwise grading is an unbounded EP mint (EP is
    # redeemable for real store items, see app/routers/store.py).
    kp_to_award = min(payload.kp_awarded, hw.kp_reward) if payload.kp_awarded is not None else hw.kp_reward
    # Scale KP by score (20 = max)
    kp_earned = int(kp_to_award * (payload.score / 20.0))

    grade = HomeworkGrade(
        homework_id=homework_id,
        score=payload.score,
        feedback=payload.feedback,
        kp_awarded=kp_earned,
    )
    db.add(grade)

    hw.status = HomeworkStatus.graded
    hw.updated_at = datetime.utcnow()
    db.add(hw)
    db.commit()

    # Award KP to student
    if kp_earned > 0:
        award_kp(
            hw.student_id,
            kp_earned,
            KpSource.homework,
            f"Devoir noté: {hw.title} ({payload.score}/20)",
            db,
        )

    from app.services.notification_engine import emit
    emit(
        db, event_type="homework_corrected", user_id=hw.student_id,
        context={"title": hw.title, "grade": f"{payload.score}/20"},
        data={"homework_id": str(hw.id), "score": payload.score},
        dedup_key=f"homework_corrected:{homework_id}",
    )

    return {
        "message": "Homework graded",
        "score": payload.score,
        "kp_awarded": kp_earned,
    }
