from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.user import User
from app.services import ai as ai_service

router = APIRouter(tags=["ai"])


class ExplainRequest(BaseModel):
    subject: str
    question: str
    level: str = "secondaire"
    stream: bool = False


class HomeworkHintRequest(BaseModel):
    homework_id: Optional[UUID] = None
    subject: str
    title: str
    statement: str
    level: str = "secondaire"


class HomeworkSolveRequest(BaseModel):
    homework_id: Optional[UUID] = None
    subject: str
    title: str
    statement: str
    level: str = "secondaire"


class QuizGenerateRequest(BaseModel):
    subject: str
    level: str
    num_questions: int = 5


class QuizEvaluateRequest(BaseModel):
    question: str
    user_answer: str
    correct_answer: str


@router.post("/explain")
async def explain_concept(
    payload: ExplainRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Explain an educational concept using Claude AI."""
    if payload.stream:
        def generate():
            for chunk in ai_service.explain_concept_stream(
                payload.subject, payload.question, payload.level
            ):
                yield chunk

        return StreamingResponse(generate(), media_type="text/plain")

    explanation = ai_service.explain_concept(payload.subject, payload.question, payload.level)
    return {"explanation": explanation, "subject": payload.subject, "level": payload.level}


@router.post("/homework/hint")
async def get_homework_hint(
    payload: HomeworkHintRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Get a hint for a homework problem."""
    hint = ai_service.generate_homework_hint({
        "subject": payload.subject,
        "title": payload.title,
        "statement": payload.statement,
        "level": payload.level,
    })
    return {"hint": hint}


@router.post("/homework/solve")
async def solve_homework(
    payload: HomeworkSolveRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Get a full solution for a homework problem."""
    solution = ai_service.solve_homework({
        "subject": payload.subject,
        "title": payload.title,
        "statement": payload.statement,
        "level": payload.level,
    })
    return {"solution": solution}


@router.post("/practice/generate")
async def generate_practice(
    payload: QuizGenerateRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Generate practice quiz questions."""
    try:
        questions = ai_service.generate_quiz(
            payload.subject, payload.level, payload.num_questions
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to generate quiz: {exc}")
    return {"questions": questions, "subject": payload.subject, "level": payload.level}


@router.post("/practice/evaluate")
async def evaluate_practice(
    payload: QuizEvaluateRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Evaluate a practice quiz answer."""
    try:
        result = ai_service.evaluate_quiz_answer(
            payload.question, payload.user_answer, payload.correct_answer
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to evaluate answer: {exc}")
    return result


@router.get("/progress-report")
async def get_progress_report(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Generate an AI progress report for the current student."""
    from app.models.booking import Booking
    from app.models.homework import Homework, HomeworkStatus
    from app.models.kp import KpAccount
    from app.models.session import Session as SessionModel, SessionStatus

    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    # Gather student data
    sessions = db.exec(
        select(SessionModel).where(
            SessionModel.student_id == user.id,
            SessionModel.status == SessionStatus.completed,
        )
    ).all()

    homeworks = db.exec(
        select(Homework).where(Homework.student_id == user.id)
    ).all()

    kp_account = db.exec(
        select(KpAccount).where(KpAccount.user_id == user.id)
    ).first()

    student_data = {
        "name": user.full_name,
        "total_sessions": len(sessions),
        "subjects_studied": list({s.subject for s in sessions if s.subject}),
        "average_rating": sum(s.rating for s in sessions if s.rating) / max(1, sum(1 for s in sessions if s.rating)),
        "homework_completed": sum(1 for h in homeworks if h.status.value == "graded"),
        "homework_total": len(homeworks),
        "kp_level": kp_account.level if kp_account else 1,
        "kp_balance": kp_account.balance if kp_account else 0,
    }

    report = ai_service.generate_progress_report(student_data)
    return {"report": report, "student_name": user.full_name}


@router.get("/session-summary/{session_id}")
async def get_session_ai_summary(
    session_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Generate or retrieve an AI summary for a session."""
    from app.models.session import Session as SessionModel
    from datetime import datetime

    session = db.get(SessionModel, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    stmt = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(stmt).first()
    if user and session.student_id != user.id and session.teacher_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    if session.ai_summary:
        return {"summary": session.ai_summary, "generated_fresh": False}

    session_data = {
        "subject": session.subject,
        "level": session.level,
        "duration_minutes": session.duration_minutes,
        "notes": session.notes,
        "feedback": session.feedback,
        "rating": session.rating,
        "scheduled_at": session.scheduled_at.isoformat(),
    }
    summary = ai_service.generate_session_summary(session_data)

    session.ai_summary = summary
    session.updated_at = datetime.utcnow()
    db.add(session)
    db.commit()

    return {"summary": summary, "generated_fresh": True}
