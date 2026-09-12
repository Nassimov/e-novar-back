from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db, require_role
from app.models.catalog import Subject
from app.models.homework import Homework, HomeworkGrade, HomeworkStatus, HomeworkSubmission
from app.models.profile import Profile
from app.schemas.homework import (
    HomeworkCreate,
    HomeworkGradeRequest,
    HomeworkResponse,
    HomeworkSubmitRequest,
    HwFileOut,
    HwGradeOut,
    HwSubmissionOut,
)
from app.services.kp import award_kp, KpSource

router = APIRouter(tags=["homework"])


def _resolve_subject_name(db: Session, hw: Homework) -> Optional[str]:
    """Free-text label wins (a teacher typed one, or the subject isn't in the
    catalog yet); otherwise falls back to the catalog subject's name. Mirrors
    app/routers/student_homework.py's identical fallback."""
    if hw.subject_name:
        return hw.subject_name
    if hw.subject_id:
        subj = db.get(Subject, hw.subject_id)
        return subj.name if subj else None
    return None


def _parse_files(raw) -> List[HwFileOut]:
    if not raw:
        return []
    try:
        return [HwFileOut(**f) if isinstance(f, dict) else f for f in raw]
    except Exception:
        return []


def _to_response(
    db: Session,
    hw: Homework,
    *,
    student_name: Optional[str] = None,
    submission: Optional[HomeworkSubmission] = None,
    grade: Optional[HomeworkGrade] = None,
) -> HomeworkResponse:
    """student_name/submission/grade can be pre-fetched (batch queries in
    list_homework) or left None to be looked up here (get_homework, create,
    update — single-row paths where a batch query would be overkill)."""
    if student_name is None:
        student = db.get(Profile, hw.student_id)
        student_name = (student.full_name if student else None) or None

    if submission is None:
        submission = db.exec(
            select(HomeworkSubmission).where(HomeworkSubmission.homework_id == hw.id)
        ).first()
    submission_data = (
        HwSubmissionOut(
            text=submission.text,
            files=_parse_files(submission.files),
            submitted_at=submission.submitted_at,
        )
        if submission
        else None
    )

    if grade is None:
        grade = db.exec(
            select(HomeworkGrade).where(HomeworkGrade.homework_id == hw.id)
        ).first()
    grade_data = (
        HwGradeOut(
            score=grade.score,
            feedback=grade.feedback,
            files=_parse_files(grade.files),
            kp_awarded=grade.kp_awarded,
            graded_at=grade.graded_at,
        )
        if grade
        else None
    )

    return HomeworkResponse(
        id=hw.id, teacher_id=hw.teacher_id, student_id=hw.student_id,
        student_name=student_name,
        session_id=hw.session_id, subject_name=_resolve_subject_name(db, hw),
        title=hw.title, statement=hw.statement, hints=hw.hints or [],
        due_at=hw.due_at, due_label=hw.due_label,
        status=hw.status.value if hasattr(hw.status, "value") else hw.status,
        kp_reward=hw.kp_reward, created_at=hw.created_at, updated_at=hw.updated_at,
        submission=submission_data, grade=grade_data,
    )


@router.get("/", response_model=Dict)
def list_homework(
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

    hw_ids = [h.id for h in paginated]
    students_map: Dict[UUID, str] = {}
    submissions_map: Dict[UUID, HomeworkSubmission] = {}
    grades_map: Dict[UUID, HomeworkGrade] = {}
    if hw_ids:
        student_ids = list({h.student_id for h in paginated})
        students = db.exec(select(Profile).where(Profile.id.in_(student_ids))).all()
        students_map = {p.id: (p.full_name or "Élève") for p in students}

        subs = db.exec(
            select(HomeworkSubmission).where(HomeworkSubmission.homework_id.in_(hw_ids))
        ).all()
        submissions_map = {s.homework_id: s for s in subs}

        grades = db.exec(
            select(HomeworkGrade).where(HomeworkGrade.homework_id.in_(hw_ids))
        ).all()
        grades_map = {g.homework_id: g for g in grades}

    return {
        "items": [
            _to_response(
                db, h,
                student_name=students_map.get(h.student_id),
                submission=submissions_map.get(h.id),
                grade=grades_map.get(h.id),
            )
            for h in paginated
        ],
        "total": total,
        "page": page,
        "size": size,
        "pages": math.ceil(total / size) if total else 0,
    }


@router.get("/{homework_id}", response_model=HomeworkResponse)
def get_homework(
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

    return _to_response(db, hw)


@router.post("/", response_model=HomeworkResponse, status_code=status.HTTP_201_CREATED)
def create_homework(
    payload: HomeworkCreate,
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Create a homework assignment (teacher only) — requires an existing
    session with that student (any status: a booking having existed is what
    establishes the relationship, not a specific session's outcome)."""
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

    if payload.session_id is not None:
        session = db.get(TutoringSession, payload.session_id)
        if session is None or session.teacher_id != teacher.id or session.student_id != payload.student_id:
            raise HTTPException(status_code=400, detail="Session invalide pour cet élève")

    # Admin-configurable homework rules (migration 113) — previously a
    # hardcoded le=500 cap with no daily-volume limit at all.
    from app.services.pricing import get_platform_settings
    platform_settings = get_platform_settings(db)
    if payload.kp_reward > platform_settings.homework_kp_reward_max:
        raise HTTPException(
            status_code=422,
            detail=f"La récompense EP ne peut pas dépasser {platform_settings.homework_kp_reward_max} pour un devoir.",
        )

    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    homeworks_today = db.exec(
        select(Homework).where(
            Homework.teacher_id == teacher.id,
            Homework.student_id == payload.student_id,
            Homework.created_at >= today_start,
        )
    ).all()
    if len(homeworks_today) >= platform_settings.homework_max_per_student_per_day:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Vous avez déjà assigné {len(homeworks_today)} devoir(s) à cet élève aujourd'hui "
                f"(maximum {platform_settings.homework_max_per_student_per_day} par jour)."
            ),
        )

    hw = Homework(
        teacher_id=teacher.id,
        student_id=payload.student_id,
        session_id=payload.session_id,
        subject_id=payload.subject_id,
        subject_name=payload.subject_name,
        title=payload.title,
        statement=payload.statement,
        hints=payload.hints,
        hints_checked=[False] * len(payload.hints),
        due_at=payload.due_at,
        due_label=payload.due_label,
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

    return _to_response(db, hw)


@router.put("/{homework_id}", response_model=HomeworkResponse)
def update_homework(
    homework_id: UUID,
    payload: HomeworkCreate,
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Update a homework assignment."""
    hw = db.get(Homework, homework_id)
    if hw is None:
        raise HTTPException(status_code=404, detail="Homework not found")

    user = db.get(Profile, UUID(current_user["id"]))
    if user is None or hw.teacher_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    hw.subject_id = payload.subject_id
    hw.subject_name = payload.subject_name
    hw.title = payload.title
    hw.statement = payload.statement
    hw.hints = payload.hints
    hw.due_at = payload.due_at
    hw.due_label = payload.due_label
    hw.kp_reward = payload.kp_reward
    hw.updated_at = datetime.utcnow()
    db.add(hw)
    db.commit()
    db.refresh(hw)

    return _to_response(db, hw)


@router.post("/{homework_id}/submit")
def submit_homework(
    homework_id: UUID,
    payload: HomeworkSubmitRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Submit a homework answer (student only). Prefer POST
    /api/student/homework/{id}/submit (app/routers/student_homework.py) —
    that is the endpoint the app's own homework UI actually calls; this one
    is kept for API completeness/back-compat."""
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
        student_id=user.id,
        text=payload.text,
        files=[{"name": "attachment", "url": payload.attachment_url, "type": "", "size": 0}] if payload.attachment_url else [],
    )
    db.add(submission)

    hw.status = HomeworkStatus.submitted
    hw.updated_at = datetime.utcnow()
    db.add(hw)
    db.commit()
    return {"message": "Homework submitted successfully"}


@router.post("/{homework_id}/grade")
def grade_homework(
    homework_id: UUID,
    payload: HomeworkGradeRequest,
    current_user: Dict[str, Any] = Depends(require_role("teacher")),
    db: Session = Depends(get_db),
):
    """Grade a submitted homework (teacher only)."""
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
        teacher_id=user.id,
        score=payload.score,
        feedback=payload.feedback,
        kp_awarded=kp_earned,
        files=[f.model_dump(exclude_none=True) for f in payload.files],
    )
    db.add(grade)

    hw.status = HomeworkStatus.graded
    hw.updated_at = datetime.utcnow()
    db.add(hw)
    db.commit()
    db.refresh(grade)

    if kp_earned > 0:
        # ref_type/ref_id makes this idempotent per homework — a retried
        # request can't award KP twice for the same grade (see
        # app/services/kp.py). A stray DB trigger used to ALSO award KP for
        # this same event with a different formula (migration 109 removed
        # it) — this was the one real double-award bug the EP audit found.
        award_kp(
            hw.student_id,
            kp_earned,
            KpSource.homework,
            f"Devoir noté: {hw.title} ({payload.score}/20)",
            db,
            ref_type="homework_grade",
            ref_id=grade.id,
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
